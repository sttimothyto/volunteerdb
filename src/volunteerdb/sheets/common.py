"""Shared roster-spreadsheet format: one sheet, one row per membership,
round-trippable (export → edit → import)."""

from ..models import ROLE_LABELS, TeamRole

ROSTER_SHEET = "Roster"
ROSTER_HEADERS = [
    "ID",
    "First name",
    "Last name",
    "Email",
    "Phone",
    "Volunteer notes",
    "Team",
    "Role",
]

# A cell opening with one of these is evaluated as a formula by Excel and
# LibreOffice, so the exporter quotes it and the importer unquotes it. Kept here
# so the two halves cannot drift apart. Leading whitespace is deliberately not
# included: the importer strips it before it could ever be interpreted.
FORMULA_STARTERS = ("=", "+", "-", "@")


def safe_cell(value):
    """Strings starting with a formula character would become live formulas when
    opened in a spreadsheet program; prefix a quote (stripped again by the
    importer on round-trip). '+' matters as much as '=': a phone number written
    '+1 416 555 0100' otherwise opens as arithmetic."""
    if isinstance(value, str) and value.startswith(FORMULA_STARTERS):
        return "'" + value
    return value


def clean_cell(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if len(text) > 1 and text[0] == "'" and text[1] in FORMULA_STARTERS:
        text = text[1:]  # undo the exporter's formula-injection escape
    return text or None


_ROLE_LOOKUP: dict[str, TeamRole] = {
    **{role.value: role for role in TeamRole},
    **{label.lower(): role for role, label in ROLE_LABELS.items()},
}


def parse_role(raw: str) -> TeamRole | None:
    """Accepts the short value ('leader') or the display label ('Ministry leader')."""
    return _ROLE_LOOKUP.get(str(raw).strip().lower())
