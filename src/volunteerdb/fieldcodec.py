"""JSON encodings for custom-field values, one type at a time.

Values live in Volunteer.custom (JSONB), so every field type needs an
encoding that survives JSON: integers stay ints, checkboxes stay bools, and
everything else is a canonical string — ISO 8601 for the temporal types,
plain decimal text for exact numbers, lowercase hex for UUIDs. This module
is the single place the write path (services.custom_fields.validate_value)
and the query compiler (query_lang) agree on those encodings.

Deliberately a leaf: stdlib plus models.FieldType only, so both
services.custom_fields and query_lang can import it without cycles.
"""

import re
import uuid as uuid_lib
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from .models import FieldType

# ISO-8601 durations, restricted to the timedelta-expressible subset: PnW, or
# P[nD][T[nH][nM][n[.f]S]]. Years and months are excluded on purpose — they
# have no fixed length, so they cannot round-trip through a timedelta.
_ISO_DURATION = re.compile(
    r"^P(?:(?P<weeks>\d+)W|(?=\d|T)(?:(?P<days>\d+)D)?"
    r"(?:T(?=\d)(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?)$"
)


def parse_duration(text: str) -> timedelta:
    """An ISO-8601 duration (PnW or P[nD][T[nH][nM][nS]]) as a timedelta."""
    m = _ISO_DURATION.match(text.strip())
    if m is None:
        raise ValueError(
            "must be an ISO 8601 duration like P1DT2H30M (weeks/days/hours/"
            "minutes/seconds only)"
        )
    parts = {k: v for k, v in m.groupdict().items() if v is not None}
    return timedelta(
        weeks=int(parts.get("weeks", 0)),
        days=int(parts.get("days", 0)),
        hours=int(parts.get("hours", 0)),
        minutes=int(parts.get("minutes", 0)),
        seconds=float(parts.get("seconds", 0)),
    )


def format_duration(td: timedelta) -> str:
    """The canonical PnDTnHnMnS spelling; equal durations get equal strings."""
    if td < timedelta(0):
        raise ValueError("durations cannot be negative")
    hours, rest = divmod(td.seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    out = "P"
    if td.days:
        out += f"{td.days}D"
    clock = ""
    if hours:
        clock += f"{hours}H"
    if minutes:
        clock += f"{minutes}M"
    if seconds or td.microseconds:
        secs = str(seconds)
        if td.microseconds:
            secs += f".{td.microseconds:06d}".rstrip("0")
        clock += f"{secs}S"
    if clock:
        out += f"T{clock}"
    return out if out != "P" else "PT0S"


def parse_scalar(ft: FieldType, value: Any) -> Any:
    """Normalize a raw value to its JSON encoding for `ft`, or raise ValueError.

    Messages describe the expected shape only ("must be ...") — callers
    prepend the field label. select is checked as bare text here; option
    membership is the caller's concern (it needs the definition).
    """
    match ft:
        case FieldType.text | FieldType.select:
            if not isinstance(value, str):
                raise ValueError("must be text")
            return value.strip() or None
        case FieldType.number:
            # bool subclasses int — reject it before the numeric check
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("must be a number")
            return value
        case FieldType.date:
            try:
                return date.fromisoformat(str(value).strip()).isoformat()
            except ValueError:
                raise ValueError("must be a YYYY-MM-DD date") from None
        case FieldType.checkbox:
            if not isinstance(value, bool):
                raise ValueError("must be true or false")
            return value
        case FieldType.integer:
            # ui.number hands back floats; accept the integral ones
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("must be a whole number")
            if isinstance(value, float):
                if not value.is_integer():
                    raise ValueError("must be a whole number")
                value = int(value)
            return value
        case FieldType.decimal:
            if isinstance(value, float):
                raise ValueError("must be a decimal written as text, like 12.50")
            try:
                d = Decimal(str(value).strip())
            except InvalidOperation:
                raise ValueError("must be a decimal number like 12.50") from None
            if not d.is_finite():
                raise ValueError("must be a finite decimal number")
            return str(d)
        case FieldType.timestamp:
            try:
                dt = datetime.fromisoformat(str(value).strip())
            except ValueError:
                raise ValueError(
                    "must be an ISO timestamp like 2026-08-17 10:30"
                ) from None
            if dt.tzinfo is not None:
                raise ValueError("must not include a timezone offset")
            return dt.isoformat()
        case FieldType.timestamptz:
            try:
                dt = datetime.fromisoformat(str(value).strip())
            except ValueError:
                raise ValueError(
                    "must be an ISO timestamp like 2026-08-17 10:30+02:00"
                ) from None
            if dt.tzinfo is None:
                raise ValueError("must include a timezone offset (e.g. +02:00 or Z)")
            return dt.isoformat()
        case FieldType.time:
            try:
                t = time.fromisoformat(str(value).strip())
            except ValueError:
                raise ValueError("must be a time like 09:15") from None
            if t.tzinfo is not None:
                raise ValueError("must not include a timezone offset")
            return t.isoformat()
        case FieldType.interval:
            return format_duration(parse_duration(str(value)))
        case FieldType.uuid:
            try:
                return str(uuid_lib.UUID(str(value).strip()))
            except ValueError:
                raise ValueError("must be a UUID") from None
        case _:
            raise ValueError(f"unsupported field type: {ft}")
