"""Data layer (SQLAlchemy Core) — one codebase, two databases.

Local dev uses a SQLite file. Production sets DATABASE_URL to a managed Postgres
connection string (e.g. Supabase) and the exact same code runs against it.

    DATABASE_URL=postgresql://user:pass@host:5432/dbname   # production
    (unset)                                                # local -> crm.db
"""
import os
from datetime import date, datetime

from sqlalchemy import (Column, Float, Integer, MetaData, Table, Text, create_engine,
                        func, insert, select, update)

from domain import follow_up_status, stage_order

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _engine_url():
    url = os.environ.get("DATABASE_URL")
    if url:
        # Normalize to the psycopg (v3) driver SQLAlchemy expects.
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        return url
    return "sqlite:///" + os.path.join(BASE_DIR, "crm.db")


_URL = _engine_url()
IS_SQLITE = _URL.startswith("sqlite")

_engine_kwargs = {"pool_pre_ping": True, "future": True}
if not IS_SQLITE:
    # Managed Postgres behind a pooler (e.g. Supabase Supavisor). Disabling
    # psycopg's auto-prepared-statements makes both session- and transaction-mode
    # pooling safe; recycle idle connections the pooler may drop.
    _engine_kwargs["pool_recycle"] = 300
    _engine_kwargs["connect_args"] = {"prepare_threshold": None}

engine = create_engine(_URL, **_engine_kwargs)

metadata = MetaData()

# Timestamps are stored as 'YYYY-MM-DD HH:MM:SS' text in both backends so the
# templates can slice created_at[:10] uniformly and there's no dialect-specific
# now() in the schema.
companies = Table(
    "companies", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("company_name", Text, nullable=False),
    Column("sector", Text),
    Column("province_city", Text, index=True),
    Column("address", Text),
    Column("owner_contact_name", Text),
    Column("phone", Text),
    Column("has_website", Text),
    Column("has_social_only", Text),
    Column("estimated_size", Text),
    Column("source", Text),
    Column("current_stage", Text, nullable=False, index=True),
    Column("last_contact_date", Text),
    Column("next_follow_up_date", Text, index=True),
    Column("notes", Text),
    Column("deal_value_dop", Float),
    Column("outcome_reason", Text),
    Column("created_at", Text),
    Column("updated_at", Text),
)

# User-editable dropdown lists (sectors, provinces). Company rows still store the
# value as plain text; this table just drives the dropdowns and dashboard groups.
options = Table(
    "options", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("kind", Text, nullable=False, index=True),   # 'sector' | 'province'
    Column("name", Text, nullable=False),
    Column("position", Integer, nullable=False, default=0),
    Column("active", Integer, nullable=False, default=1),
)

# Company column each option kind maps to (for rename cascades / grouping).
OPTION_COLUMN = {"sector": "sector", "province": "province_city"}

# Columns writable from forms / the seed (id and timestamps are managed here).
COLUMNS = [
    "company_name", "sector", "province_city", "address", "owner_contact_name",
    "phone", "has_website", "has_social_only", "estimated_size", "source",
    "current_stage", "last_contact_date", "next_follow_up_date", "notes",
    "deal_value_dop", "outcome_reason",
]


def init_db():
    """Create the schema if it doesn't exist, and seed the option lists once."""
    metadata.create_all(engine)
    from domain import SECTORS as _SEC, PROVINCES as _PROV
    defaults = {"sector": _SEC, "province": _PROV}
    with engine.begin() as conn:
        for kind, names in defaults.items():
            exists = conn.execute(
                select(func.count()).select_from(options).where(options.c.kind == kind)
            ).scalar_one()
            if not exists:
                conn.execute(insert(options), [
                    {"kind": kind, "name": n, "position": i, "active": 1}
                    for i, n in enumerate(names)
                ])


# --- Option-list helpers ---------------------------------------------------

def get_options(conn, kind, active_only=True):
    """Ordered list of option names for a kind (used to build dropdowns)."""
    stmt = select(options.c.name).where(options.c.kind == kind)
    if active_only:
        stmt = stmt.where(options.c.active == 1)
    stmt = stmt.order_by(options.c.position, options.c.id)
    return list(conn.execute(stmt).scalars().all())


def options_of(kind):
    """Convenience: open a short-lived connection and return option names."""
    with engine.connect() as conn:
        return get_options(conn, kind)


def list_option_rows(conn, kind):
    """Active option rows (id + name) for the settings management page."""
    stmt = (select(options.c.id, options.c.name)
            .where(options.c.kind == kind, options.c.active == 1)
            .order_by(options.c.position, options.c.id))
    return [dict(r) for r in conn.execute(stmt).mappings().all()]


def add_option(conn, kind, name):
    if not name:
        return
    existing = get_options(conn, kind)
    if name.lower() in {e.lower() for e in existing}:
        return  # already present (case-insensitive)
    max_pos = conn.execute(
        select(func.coalesce(func.max(options.c.position), -1)).where(options.c.kind == kind)
    ).scalar_one()
    conn.execute(insert(options).values(
        kind=kind, name=name, position=max_pos + 1, active=1))


def rename_option(conn, kind, option_id, new_name):
    if not new_name:
        return
    old = conn.execute(
        select(options.c.name).where(options.c.id == option_id, options.c.kind == kind)
    ).scalar_one_or_none()
    if old is None or old == new_name:
        if old is None:
            return
    conn.execute(update(options).where(options.c.id == option_id).values(name=new_name))
    # Cascade to existing companies so the rename doesn't orphan their values.
    col = OPTION_COLUMN[kind]
    conn.execute(update(companies).where(companies.c[col] == old).values(**{col: new_name}))


def remove_option(conn, kind, option_id):
    """Soft-remove: hide from dropdowns; companies keep their text value."""
    conn.execute(update(options).where(
        options.c.id == option_id, options.c.kind == kind).values(active=0))


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _enrich(row, today=None):
    """Turn a result mapping into a dict plus the computed fields."""
    d = dict(row)
    d["follow_up_status"] = follow_up_status(d.get("next_follow_up_date"), today)
    d["stage_order"] = stage_order(d.get("current_stage"))
    return d


def insert_company(conn, data):
    values = {c: data[c] for c in COLUMNS if c in data}
    now = _now()
    values.setdefault("current_stage", "Identified")
    values["created_at"] = now
    values["updated_at"] = now
    result = conn.execute(insert(companies).values(**values))
    return result.inserted_primary_key[0]


def update_company(conn, company_id, data):
    values = {c: data[c] for c in COLUMNS if c in data}
    values["updated_at"] = _now()
    conn.execute(update(companies).where(companies.c.id == company_id).values(**values))


def get_company(conn, company_id, today=None):
    row = conn.execute(
        select(companies).where(companies.c.id == company_id)
    ).mappings().first()
    return _enrich(row, today) if row else None


def list_companies(conn, province=None, sector=None, stage=None, search=None,
                   sort="updated_at", today=None):
    stmt = select(companies)
    if province:
        stmt = stmt.where(companies.c.province_city == province)
    if sector:
        stmt = stmt.where(companies.c.sector == sector)
    if stage:
        stmt = stmt.where(companies.c.current_stage == stage)
    if search:
        stmt = stmt.where(companies.c.company_name.ilike(f"%{search}%"))

    if sort == "name":
        stmt = stmt.order_by(func.lower(companies.c.company_name).asc())
    elif sort == "next_follow_up":
        stmt = stmt.order_by(companies.c.next_follow_up_date.is_(None),
                             companies.c.next_follow_up_date.asc())
    elif sort == "stage":
        stmt = stmt.order_by(companies.c.current_stage.asc())
    else:
        stmt = stmt.order_by(companies.c.updated_at.desc())

    return [_enrich(r, today) for r in conn.execute(stmt).mappings().all()]


def count_companies(conn):
    return conn.execute(select(func.count()).select_from(companies)).scalar_one()


def dashboard_metrics(conn, today=None):
    """Recompute every Dashboard number live, matching the spreadsheet logic."""
    if today is None:
        from domain import today as _dr_today
        today = _dr_today()
    rows = [_enrich(r, today) for r in conn.execute(select(companies)).mappings().all()]

    from domain import FUNNEL_STAGES, stage_order as so

    total = len(rows)
    lost = [r for r in rows if r["current_stage"] == "Lost"]
    non_lost = [r for r in rows if r["current_stage"] != "Lost"]

    funnel = []
    prev_cumulative = None
    for i, stage in enumerate(FUNNEL_STAGES):
        n = i + 1
        current = sum(1 for r in rows if r["current_stage"] == stage)
        cumulative = sum(1 for r in non_lost if so(r["current_stage"]) >= n)
        conversion = None
        if prev_cumulative not in (None, 0):
            conversion = cumulative / prev_cumulative
        funnel.append({"stage": stage, "current": current,
                       "cumulative": cumulative, "conversion": conversion})
        prev_cumulative = cumulative

    identified_reached = funnel[0]["cumulative"]
    lost_rate = None
    denom = identified_reached + len(lost)
    if denom:
        lost_rate = len(lost) / denom

    # Group by the live option lists, plus any values present on companies that
    # aren't in the list anymore (e.g. after a removal) so no count is lost.
    def _grouped(col, kind):
        names = get_options(conn, kind)
        present = sorted({r[col] for r in rows if r[col]})
        names = names + [v for v in present if v not in names]
        return {n: sum(1 for r in rows if r[col] == n) for n in names}

    by_province = _grouped("province_city", "province")
    by_sector = _grouped("sector", "sector")

    follow = {"OVERDUE": 0, "DUE TODAY": 0, "Upcoming": 0}
    for r in rows:
        st = r["follow_up_status"]
        if st in follow:
            follow[st] += 1

    won = [r for r in rows if r["current_stage"] == "Won"]
    won_values = [r["deal_value_dop"] for r in won if r["deal_value_dop"] is not None]
    won_total = sum(won_values) if won_values else 0
    won_avg = (won_total / len(won_values)) if won_values else None

    return {
        "total": total, "funnel": funnel,
        "lost_count": len(lost), "lost_rate": lost_rate,
        "by_province": by_province, "by_sector": by_sector, "follow": follow,
        "won_count": len(won), "won_total": won_total, "won_avg": won_avg,
    }
