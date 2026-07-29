"""Domain constants and computed fields for the DR SMB Outreach CRM.

Single source of truth for the enums (matching DR_Outreach_Tracker.xlsx Lists tab)
and the two computed fields the spreadsheet recomputes live (follow_up_status,
stage_order). Imported by both the Flask app and the seed import script.
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

# The app is used from the Dominican Republic (UTC-4, no DST). Production runs on
# Render in UTC, so we must anchor "today" to DR time — otherwise in the evening
# the server's date is already tomorrow and follow-ups read a day off.
LOCAL_TZ = ZoneInfo("America/Santo_Domingo")


def today():
    """Today's date in the founder's timezone (not the server's UTC)."""
    return datetime.now(LOCAL_TZ).date()

# --- Enums (exact values, order matters for pipeline stages) ---------------

SECTORS = [
    "Ferretería", "Colmado", "Salón de belleza", "Taller mecánico",
    "Farmacia independiente", "Restaurante", "Tienda de ropa",
    "Agencia de viaje", "Gestoría", "Colegio", "Clínica Dental",
    "Centro Médico/Clínica Privada", "Otro",
]

PROVINCES = ["San Cristóbal", "Santo Domingo", "Punta Cana/Bávaro", "Otro"]

SIZES = ["Small", "Medium", "Large"]

SOURCES = [
    "Google Maps", "Instagram", "Facebook", "Referral", "Drive-by",
    "Cold call", "Cold message", "Otro",
]

YN = ["Y", "N"]

# Linear pipeline, in order. Lost is a terminal state reachable from any stage.
STAGES = [
    "Identified", "Verified", "Contacted", "Demo Scheduled",
    "Demo Delivered", "Negotiating", "Won", "Lost",
]

# Stages that make up the funnel (everything except Lost).
FUNNEL_STAGES = STAGES[:7]

STAGE_ORDER = {stage: i + 1 for i, stage in enumerate(STAGES)}  # Identified=1 .. Lost=8

# Sane defaults per the plan analysis (San Cristóbal first, DOP pricing).
DEFAULT_PROVINCE = "San Cristóbal"
DEFAULT_STAGE = "Identified"
CURRENCY_LABEL = "RD$"  # Dominican pesos (DOP)


def stage_order(stage):
    """Numeric rank used only for funnel math; never shown to the user."""
    return STAGE_ORDER.get(stage, 0)


def follow_up_status(next_follow_up_date, ref_today=None):
    """Compute follow-up status from the next follow-up date vs. today (DR time).

    None date          -> None (blank/none)
    date before today  -> 'OVERDUE'
    date equals today  -> 'DUE TODAY'
    date after today   -> 'Upcoming'
    """
    if not next_follow_up_date:
        return None
    if ref_today is None:
        ref_today = today()
    d = _parse_date(next_follow_up_date)
    if d is None:
        return None
    if d < ref_today:
        return "OVERDUE"
    if d == ref_today:
        return "DUE TODAY"
    return "Upcoming"


def _parse_date(value):
    """Parse an ISO 'YYYY-MM-DD' string (or passthrough a date) into a date."""
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None
