"""JSON encodings for custom-field values, one type at a time.

Values live in Volunteer.custom (JSONB), so every field type needs an
encoding that survives JSON: integers stay ints, checkboxes stay bools, and
everything else is a canonical string — ISO 8601 for the temporal types,
plain decimal text for exact numbers, lowercase hex for UUIDs. This module
is the single place the write path (services.custom_fields.validate_value)
and the query compiler (query_lang) agree on those encodings.

Deliberately a leaf: stdlib, models.FieldType and the Result types only, so
both services.custom_fields and query_lang can import it without cycles. A
value that does not fit is an Err[Invalid] whose message names the expected
shape ("must be ..."); the callers prepend the field's label.
"""

import re
import uuid as uuid_lib
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from .errors import Invalid, invalid
from .fp import Err, Ok, Result
from .models import FieldType

# ISO-8601 durations, restricted to the timedelta-expressible subset: PnW, or
# P[nD][T[nH][nM][n[.f]S]]. Years and months are excluded on purpose — they
# have no fixed length, so they cannot round-trip through a timedelta.
_ISO_DURATION = re.compile(
    r"^P(?:(?P<weeks>\d+)W|(?=\d|T)(?:(?P<days>\d+)D)?"
    r"(?:T(?=\d)(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?)$"
)


def parse_duration(text: str) -> Result[timedelta, Invalid]:
    """An ISO-8601 duration (PnW or P[nD][T[nH][nM][nS]]) as a timedelta."""
    m = _ISO_DURATION.match(text.strip())
    if m is None:
        return invalid(
            "must be an ISO 8601 duration like P1DT2H30M (weeks/days/hours/"
            "minutes/seconds only)"
        )
    parts = {k: v for k, v in m.groupdict().items() if v is not None}
    return Ok(
        timedelta(
            weeks=int(parts.get("weeks", 0)),
            days=int(parts.get("days", 0)),
            hours=int(parts.get("hours", 0)),
            minutes=int(parts.get("minutes", 0)),
            seconds=float(parts.get("seconds", 0)),
        )
    )


def format_duration(td: timedelta) -> str:
    """The canonical PnDTnHnMnS spelling; equal durations get equal strings.
    Non-negative by contract: parse_duration never yields a negative one."""
    assert td >= timedelta(0), "durations cannot be negative"
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


def parse_scalar(ft: FieldType, value: Any) -> Result[Any, Invalid]:
    """Normalize a raw value to its JSON encoding for `ft`, or the Invalid
    naming the shape expected.

    Messages describe the expected shape only ("must be ...") — callers
    prepend the field label. select is checked as bare text here; option
    membership is the caller's concern (it needs the definition). A blank
    text normalizes to None, which the write path reads as "clear".
    """
    match ft:
        case FieldType.text | FieldType.select:
            if not isinstance(value, str):
                return invalid("must be text")
            return Ok(value.strip() or None)
        case FieldType.number:
            # bool subclasses int — reject it before the numeric check
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return invalid("must be a number")
            return Ok(value)
        case FieldType.date:
            try:
                return Ok(date.fromisoformat(str(value).strip()).isoformat())
            except ValueError:
                return invalid("must be a YYYY-MM-DD date")
        case FieldType.checkbox:
            if not isinstance(value, bool):
                return invalid("must be true or false")
            return Ok(value)
        case FieldType.integer:
            # ui.number hands back floats; accept the integral ones
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return invalid("must be a whole number")
            if isinstance(value, float):
                if not value.is_integer():
                    return invalid("must be a whole number")
                value = int(value)
            return Ok(value)
        case FieldType.decimal:
            if isinstance(value, float):
                return invalid("must be a decimal written as text, like 12.50")
            try:
                d = Decimal(str(value).strip())
            except InvalidOperation:
                return invalid("must be a decimal number like 12.50")
            if not d.is_finite():
                return invalid("must be a finite decimal number")
            return Ok(str(d))
        case FieldType.timestamp:
            try:
                dt = datetime.fromisoformat(str(value).strip())
            except ValueError:
                return invalid("must be an ISO timestamp like 2026-08-17 10:30")
            if dt.tzinfo is not None:
                return invalid("must not include a timezone offset")
            return Ok(dt.isoformat())
        case FieldType.timestamptz:
            try:
                dt = datetime.fromisoformat(str(value).strip())
            except ValueError:
                return invalid("must be an ISO timestamp like 2026-08-17 10:30+02:00")
            if dt.tzinfo is None:
                return invalid("must include a timezone offset (e.g. +02:00 or Z)")
            return Ok(dt.isoformat())
        case FieldType.time:
            try:
                t = time.fromisoformat(str(value).strip())
            except ValueError:
                return invalid("must be a time like 09:15")
            if t.tzinfo is not None:
                return invalid("must not include a timezone offset")
            return Ok(t.isoformat())
        case FieldType.interval:
            parsed = parse_duration(str(value))
            if isinstance(parsed, Err):
                return parsed
            return Ok(format_duration(parsed.value))
        case FieldType.uuid:
            try:
                return Ok(str(uuid_lib.UUID(str(value).strip())))
            except ValueError:
                return invalid("must be a UUID")
        case _:
            return invalid(f"unsupported field type: {ft}")
