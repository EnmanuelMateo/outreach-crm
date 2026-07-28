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

# Columns writable from forms / the seed (id and timestamps are managed here).
COLUMNS = [
    "company_name", "sector", "province_city", "address", "owner_contact_name",
    "phone", "has_website", "has_social_only", "estimated_size", "source",
    "current_stage", "last_contact_date", "next_follow_up_date", "notes",
    "deal_value_dop", "outcome_reason",
]


def init_db():
    """Create the schema if it doesn't exist. Safe to run repeatedly."""
    metadata.create_all(engine)


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
        today = date.today()
    rows = [_enrich(r, today) for r in conn.execute(select(companies)).mappings().all()]

    from domain import FUNNEL_STAGES, SECTORS, PROVINCES, stage_order as so

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

    by_province = {p: sum(1 for r in rows if r["province_city"] == p) for p in PROVINCES}
    by_sector = {s: sum(1 for r in rows if r["sector"] == s) for s in SECTORS}

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
