"""DR SMB Outreach CRM — single-user Flask app (no auth by design).

Run locally:   python app.py     (http://127.0.0.1:5000)
Production:    gunicorn app:app   (see Dockerfile / README)
"""
import csv
import io
import re
from datetime import date, timedelta

from flask import (Flask, Response, redirect, render_template, request, url_for)

import db
from domain import (SECTORS, PROVINCES, SIZES, SOURCES, YN, STAGES, FUNNEL_STAGES,
                    DEFAULT_PROVINCE, DEFAULT_STAGE, CURRENCY_LABEL, _parse_date)

app = Flask(__name__)
db.init_db()


# --- Template helpers ------------------------------------------------------

_ES_DAYS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
_ES_MONTHS = ["", "ene", "feb", "mar", "abr", "may", "jun",
              "jul", "ago", "sep", "oct", "nov", "dic"]


@app.context_processor
def inject_globals():
    t = date.today()
    return {
        "SECTORS": SECTORS, "PROVINCES": PROVINCES, "SIZES": SIZES,
        "SOURCES": SOURCES, "YN": YN, "STAGES": STAGES,
        "FUNNEL_STAGES": FUNNEL_STAGES, "CURRENCY": CURRENCY_LABEL,
        "today_iso": t.isoformat(),
        "today_label": f"{_ES_DAYS[t.weekday()]} {t.day} {_ES_MONTHS[t.month]}",
    }


@app.template_filter("wa")
def whatsapp_link(phone):
    """Build a wa.me link from a DR phone number (assumes +1 country code)."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 10:          # local 809/829/849 number
        digits = "1" + digits
    return f"https://wa.me/{digits}"


def _safe_next(value, fallback):
    """Only allow same-site relative redirects (guards the ?next param)."""
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return fallback


@app.template_filter("dop")
def format_dop(value):
    if value is None or value == "":
        return "—"
    try:
        return f"{CURRENCY_LABEL}{float(value):,.0f}"
    except (ValueError, TypeError):
        return "—"


@app.template_filter("pct")
def format_pct(value):
    if value is None:
        return "—"
    return f"{value * 100:.0f}%"


# --- Form parsing ----------------------------------------------------------

def _clean(value):
    """Empty string -> None; strip whitespace otherwise."""
    if value is None:
        return None
    value = value.strip()
    return value or None


def _validate_enum(value, allowed):
    return value if value in allowed else None


def parse_company_form(form):
    """Turn submitted form fields into a validated column dict."""
    deal_raw = _clean(form.get("deal_value_dop"))
    deal_value = None
    if deal_raw is not None:
        try:
            deal_value = float(deal_raw.replace(",", "").replace(CURRENCY_LABEL, "").strip())
        except ValueError:
            deal_value = None

    stage = _validate_enum(_clean(form.get("current_stage")), STAGES) or DEFAULT_STAGE

    data = {
        "company_name": _clean(form.get("company_name")) or "(sin nombre)",
        "sector": _validate_enum(_clean(form.get("sector")), SECTORS),
        "province_city": _validate_enum(_clean(form.get("province_city")), PROVINCES),
        "address": _clean(form.get("address")),
        "owner_contact_name": _clean(form.get("owner_contact_name")),
        "phone": _clean(form.get("phone")),
        "has_website": _validate_enum(_clean(form.get("has_website")), YN),
        "has_social_only": _validate_enum(_clean(form.get("has_social_only")), YN),
        "estimated_size": _validate_enum(_clean(form.get("estimated_size")), SIZES),
        "source": _validate_enum(_clean(form.get("source")), SOURCES),
        "current_stage": stage,
        "last_contact_date": _clean(form.get("last_contact_date")),
        "next_follow_up_date": _clean(form.get("next_follow_up_date")),
        "notes": _clean(form.get("notes")),
        "deal_value_dop": deal_value,
        "outcome_reason": _clean(form.get("outcome_reason")),
    }
    return data


# Writable columns used for the CSV import template (order = template header).
# Matches the export so an exported file re-imports cleanly (extra columns like
# id / follow_up_status / created_at / updated_at are simply ignored).
IMPORT_COLUMNS = [
    "company_name", "sector", "province_city", "address", "owner_contact_name",
    "phone", "has_website", "has_social_only", "estimated_size", "source",
    "current_stage", "last_contact_date", "next_follow_up_date", "deal_value_dop",
    "outcome_reason", "notes",
]


def _norm_date(value):
    """Normalize a CSV date cell to ISO 'YYYY-MM-DD', or None if unparseable."""
    d = _parse_date(value)
    return d.isoformat() if d else None


def parse_csv_row(row):
    """Map one CSV row (dict keyed by header) to a validated company dict.
    Unknown enum values become None; company_name is left None if missing so the
    caller can skip the row. Reuses the same validators as the Add form."""
    def g(key):
        return _clean(row.get(key))

    deal_raw = g("deal_value_dop")
    deal_value = None
    if deal_raw is not None:
        try:
            deal_value = float(deal_raw.replace(",", "").replace(CURRENCY_LABEL, "").strip())
        except ValueError:
            deal_value = None

    return {
        "company_name": g("company_name"),
        "sector": _validate_enum(g("sector"), SECTORS),
        "province_city": _validate_enum(g("province_city"), PROVINCES),
        "address": g("address"),
        "owner_contact_name": g("owner_contact_name"),
        "phone": g("phone"),
        "has_website": _validate_enum(g("has_website"), YN),
        "has_social_only": _validate_enum(g("has_social_only"), YN),
        "estimated_size": _validate_enum(g("estimated_size"), SIZES),
        "source": _validate_enum(g("source"), SOURCES),
        "current_stage": _validate_enum(g("current_stage"), STAGES) or DEFAULT_STAGE,
        "last_contact_date": _norm_date(g("last_contact_date")),
        "next_follow_up_date": _norm_date(g("next_follow_up_date")),
        "notes": g("notes"),
        "deal_value_dop": deal_value,
        "outcome_reason": g("outcome_reason"),
    }


# --- Routes ----------------------------------------------------------------

@app.route("/")
@app.route("/dashboard")
def dashboard():
    with db.engine.connect() as conn:
        metrics = db.dashboard_metrics(conn)
    return render_template("dashboard.html", m=metrics, active="dashboard")


@app.route("/companies")
def companies():
    f = {
        "province": request.args.get("province") or "",
        "sector": request.args.get("sector") or "",
        "stage": request.args.get("stage") or "",
        "search": request.args.get("search") or "",
        "sort": request.args.get("sort") or "updated_at",
    }
    open_id = request.args.get("open", type=int)
    with db.engine.connect() as conn:
        rows = db.list_companies(
            conn, province=f["province"] or None, sector=f["sector"] or None,
            stage=f["stage"] or None, search=f["search"] or None, sort=f["sort"])
        selected = db.get_company(conn, open_id) if open_id else None
    return render_template("companies.html", rows=rows, f=f, selected=selected,
                           active="companies")


@app.route("/companies/new", methods=["GET", "POST"])
def company_new():
    if request.method == "POST":
        data = parse_company_form(request.form)
        with db.engine.begin() as conn:
            new_id = db.insert_company(conn, data)
        # "Guardar y añadir otra" loops back to a fresh form for fast field entry.
        if request.form.get("and_another"):
            return redirect(url_for("company_new"))
        return redirect(url_for("company_detail", company_id=new_id))
    # GET: full blank record (every field the template reads) + sensible defaults.
    default = {col: None for col in db.COLUMNS}
    default["id"] = None
    default.update({
        "current_stage": DEFAULT_STAGE,
        "province_city": DEFAULT_PROVINCE,
        "has_website": "N",
        "last_contact_date": date.today().isoformat(),
        "next_follow_up_date": (date.today() + timedelta(days=3)).isoformat(),
    })
    return render_template("company_form.html", c=default, mode="new", active="new")


@app.route("/companies/<int:company_id>")
def company_detail(company_id):
    with db.engine.connect() as conn:
        company = db.get_company(conn, company_id)
    if not company:
        return redirect(url_for("companies"))
    return render_template("company_detail.html", c=company, active="companies")


@app.route("/companies/<int:company_id>/edit", methods=["GET", "POST"])
def company_edit(company_id):
    if request.method == "POST":
        data = parse_company_form(request.form)
        with db.engine.begin() as conn:
            db.update_company(conn, company_id, data)
        return redirect(_safe_next(
            request.form.get("next"),
            url_for("company_detail", company_id=company_id)))
    with db.engine.connect() as conn:
        company = db.get_company(conn, company_id)
    if not company:
        return redirect(url_for("companies"))
    return render_template("company_form.html", c=company, mode="edit", active="companies")


@app.route("/companies/<int:company_id>/advance", methods=["POST"])
def company_advance(company_id):
    """The daily pipeline action: move stage, optionally log a note, set the next
    follow-up, and capture deal value (Won) or outcome reason (Lost)."""
    with db.engine.begin() as conn:
        company = db.get_company(conn, company_id)
        if not company:
            return redirect(url_for("companies"))

        from domain import STAGES as _S
        new_stage = request.form.get("current_stage")
        if new_stage in _S:
            company["current_stage"] = new_stage

        last_contact = _clean(request.form.get("last_contact_date"))
        if last_contact:
            company["last_contact_date"] = last_contact
        next_follow = request.form.get("next_follow_up_date")
        if next_follow is not None:
            company["next_follow_up_date"] = _clean(next_follow)

        deal_raw = _clean(request.form.get("deal_value_dop"))
        if deal_raw is not None:
            try:
                company["deal_value_dop"] = float(
                    deal_raw.replace(",", "").replace(CURRENCY_LABEL, "").strip())
            except ValueError:
                pass
        outcome = _clean(request.form.get("outcome_reason"))
        if outcome is not None:
            company["outcome_reason"] = outcome

        note_add = _clean(request.form.get("note_add"))
        if note_add:
            stamp = date.today().isoformat()
            existing = company.get("notes") or ""
            company["notes"] = (f"{existing}\n[{stamp}] {note_add}").strip()

        db.update_company(conn, company_id, company)
    return redirect(_safe_next(
        request.form.get("next"),
        url_for("company_detail", company_id=company_id)))


@app.route("/companies.csv")
def companies_csv():
    """Export the current (filtered) company list to CSV."""
    with db.engine.connect() as conn:
        rows = db.list_companies(
            conn,
            province=request.args.get("province") or None,
            sector=request.args.get("sector") or None,
            stage=request.args.get("stage") or None,
            search=request.args.get("search") or None,
            sort=request.args.get("sort") or "updated_at")

    fields = [
        "id", "company_name", "sector", "province_city", "address",
        "owner_contact_name", "phone", "has_website", "has_social_only",
        "estimated_size", "source", "current_stage", "last_contact_date",
        "next_follow_up_date", "follow_up_status", "deal_value_dop",
        "outcome_reason", "notes", "created_at", "updated_at",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(r)

    # BOM so Excel opens the accented Spanish text correctly.
    payload = "﻿" + buf.getvalue()
    filename = f"companies_{date.today().isoformat()}.csv"
    return Response(
        payload, mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.route("/companies/template.csv")
def companies_template_csv():
    """Blank import template: the writable headers + one example row."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=IMPORT_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerow({
        "company_name": "Ferretería El Progreso", "sector": "Ferretería",
        "province_city": "San Cristóbal", "address": "Av. Constitución 42",
        "owner_contact_name": "Juan Pérez", "phone": "809-555-0142",
        "has_website": "N", "has_social_only": "Y", "estimated_size": "Small",
        "source": "Drive-by", "current_stage": "Identified",
        "last_contact_date": "", "next_follow_up_date": "2026-08-15",
        "deal_value_dop": "", "outcome_reason": "",
        "notes": "Ejemplo — borra esta fila antes de importar.",
    })
    payload = "﻿" + buf.getvalue()
    return Response(
        payload, mimetype="text/csv",
        headers={"Content-Disposition": 'attachment; filename="plantilla_empresas.csv"'})


@app.route("/companies/import", methods=["GET", "POST"])
def company_import():
    if request.method == "GET":
        return render_template("company_import.html", active="companies",
                               result=None, error=None)

    file = request.files.get("file")
    if not file or not file.filename:
        return render_template("company_import.html", active="companies",
                               result=None, error="Elige un archivo CSV primero.")
    try:
        text = file.read().decode("utf-8-sig")  # utf-8-sig strips the Excel BOM
    except UnicodeDecodeError:
        text = file.read().decode("latin-1", errors="replace")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "company_name" not in reader.fieldnames:
        return render_template(
            "company_import.html", active="companies", result=None,
            error="El CSV no tiene una columna 'company_name'. Descarga la plantilla.")

    inserted, skipped_dup, errors = 0, 0, []
    with db.engine.begin() as conn:
        # Existing names (lower-cased) so re-importing the same file is a no-op.
        existing = {
            (r["company_name"] or "").strip().lower()
            for r in conn.execute(db.select(db.companies.c.company_name)).mappings()
        }
        for i, row in enumerate(reader, start=2):  # row 1 is the header
            data = parse_csv_row(row)
            name = data["company_name"]
            if not name:
                errors.append(f"Fila {i}: sin nombre de empresa — omitida.")
                continue
            key = name.strip().lower()
            if key in existing:
                skipped_dup += 1
                continue
            db.insert_company(conn, data)
            existing.add(key)
            inserted += 1

    result = {"inserted": inserted, "skipped_dup": skipped_dup, "errors": errors}
    return render_template("company_import.html", active="companies",
                           result=result, error=None)


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    # Reloader spawns a second process; off keeps a single clean process.
    app.run(host="0.0.0.0", port=port, debug=debug, use_reloader=False)
