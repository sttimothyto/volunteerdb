"""JSON encodings for custom-field values (fieldcodec)."""

from datetime import timedelta

import pytest

from volunteerdb import fieldcodec
from volunteerdb.errors import Invalid
from volunteerdb.models import FieldType

from tests.fp_helpers import ok, refused

pytestmark = pytest.mark.pure


def test_duration_round_trips():
    for text, td, canonical in [
        ("PT0S", timedelta(0), "PT0S"),
        ("P2W", timedelta(weeks=2), "P14D"),
        ("P1DT2H30M", timedelta(days=1, hours=2, minutes=30), "P1DT2H30M"),
        ("PT90M", timedelta(minutes=90), "PT1H30M"),
        ("PT1.5S", timedelta(seconds=1.5), "PT1.5S"),
        ("P3D", timedelta(days=3), "P3D"),
    ]:
        assert ok(fieldcodec.parse_duration(text)) == td
        assert fieldcodec.format_duration(td) == canonical
        # canonical spellings are fixed points
        assert fieldcodec.format_duration(ok(fieldcodec.parse_duration(canonical))) == (
            canonical
        )


def test_duration_rejects_unfixed_units_and_noise():
    for bad in ["P1Y", "P1M", "P", "PT", "3 days", "P1D2H", "PT2H30", "-P1D", ""]:
        refused(fieldcodec.parse_duration(bad), Invalid, match="ISO 8601 duration")
    with pytest.raises(AssertionError):  # a contract, not input: parse never yields one
        fieldcodec.format_duration(timedelta(days=-1))


def test_parse_scalar_handles_every_field_type():
    """Exhaustiveness: a FieldType member without a codec case must fail loudly,
    not silently clear values (validate_value treats None as 'clear')."""
    samples = {
        FieldType.text: "x",
        FieldType.number: 1.5,
        FieldType.select: "x",
        FieldType.date: "2026-01-02",
        FieldType.checkbox: True,
        FieldType.integer: 3,
        FieldType.decimal: "1.23",
        FieldType.timestamp: "2026-01-02T03:04",
        FieldType.timestamptz: "2026-01-02T03:04+00:00",
        FieldType.time: "03:04",
        FieldType.interval: "PT1H",
        FieldType.uuid: "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
    }
    assert set(samples) == set(FieldType)
    for ft, sample in samples.items():
        assert ok(fieldcodec.parse_scalar(ft, sample)) is not None
