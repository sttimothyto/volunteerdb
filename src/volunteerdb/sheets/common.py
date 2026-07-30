"""Shared spreadsheet format: one workbook, two sheets, round-trippable
(export → edit → import)."""

from ..models import ROLE_LABELS, TeamRole

VOLUNTEER_SHEET = "Volunteers"
VOLUNTEER_HEADERS = ["First name", "Last name", "Email", "Phone", "Notes", "Active"]

MEMBERSHIP_SHEET = "Memberships"
MEMBERSHIP_HEADERS = ["Volunteer email", "Volunteer name", "Team path", "Role", "Joined on", "Notes"]

# A cell opening with one of these is evaluated as a formula by Excel and
# LibreOffice, so the exporter quotes it and the importer unquotes it. Kept here
# so the two halves cannot drift apart. Leading whitespace is deliberately not
# included: the importer strips it before it could ever be interpreted.
FORMULA_STARTERS = ("=", "+", "-", "@")

_ROLE_LOOKUP: dict[str, TeamRole] = {
    **{role.value: role for role in TeamRole},
    **{label.lower(): role for role, label in ROLE_LABELS.items()},
}


def parse_role(raw: str) -> TeamRole | None:
    """Accepts the short value ('leader') or the display label ('Ministry leader')."""
    return _ROLE_LOOKUP.get(str(raw).strip().lower())
