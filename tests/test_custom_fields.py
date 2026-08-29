"""Custom field definitions and per-volunteer values (Volunteer.custom JSONB)."""

import pytest
from sqlalchemy.exc import IntegrityError

from volunteerdb import errors
from volunteerdb.models import FieldType
from volunteerdb.services import custom_fields, volunteers

from tests.conftest import db_session
from tests.fp_helpers import ok, refused


async def test_create_def_slugifies_and_orders(database):
    async with db_session() as session:
        ok(
            await custom_fields.create_def(
                session, None, "Safeguarding training", FieldType.date, position=2
            )
        )
        ok(
            await custom_fields.create_def(
                session, None, "Shirt size!", FieldType.text, position=1
            )
        )

    async with db_session() as session:
        defs = await custom_fields.list_defs(session)
        assert [d.key for d in defs] == ["shirt_size", "safeguarding_training"]
        assert defs[0].label == "Shirt size!"


async def test_duplicate_key_conflicts(database):
    async with db_session() as session:
        ok(await custom_fields.create_def(session, None, "Shirt size", FieldType.text))
    with pytest.raises(IntegrityError):
        async with db_session() as session:
            ok(
                await custom_fields.create_def(
                    session, None, "shirt SIZE?", FieldType.number
                )
            )


async def test_select_requires_options(database):
    async with db_session() as session:
        refused(
            await custom_fields.create_def(
                session, None, "Preferred contact", FieldType.select
            ),
            errors.Invalid,
        )
        refused(
            await custom_fields.create_def(
                session, None, "Preferred contact", FieldType.select, options=["", "  "]
            ),
            errors.Invalid,
        )


async def test_update_def_replaces_options_and_deactivates(database):
    async with db_session() as session:
        defn = ok(
            await custom_fields.create_def(
                session,
                None,
                "Preferred contact",
                FieldType.select,
                options=["Email", "Phone"],
            )
        )
        fid = defn.id

    async with db_session() as session:
        ok(
            await custom_fields.update_def(
                session,
                None,
                fid,
                label="Contact preference",
                options=["Email", "Phone", "Post"],
            )
        )
        ok(await custom_fields.update_def(session, None, fid, is_active=False))

    async with db_session() as session:
        assert await custom_fields.list_defs(session) == []
        (defn,) = await custom_fields.list_defs(session, include_inactive=True)
        assert defn.label == "Contact preference"
        assert defn.options == ["Email", "Phone", "Post"]
        assert defn.key == "preferred_contact"  # key is immutable

    async with db_session() as session:  # options only make sense on select fields
        text_def = ok(
            await custom_fields.create_def(session, None, "Notes 2", FieldType.text)
        )
        refused(
            await custom_fields.update_def(session, None, text_def.id, options=["a"]),
            errors.Invalid,
        )


async def test_validate_value_matrix(database):
    async with db_session() as session:
        text_d = ok(await custom_fields.create_def(session, None, "T", FieldType.text))
        num_d = ok(await custom_fields.create_def(session, None, "N", FieldType.number))
        sel_d = ok(
            await custom_fields.create_def(
                session, None, "S", FieldType.select, options=["a", "b"]
            )
        )
        date_d = ok(await custom_fields.create_def(session, None, "D", FieldType.date))
        check_d = ok(
            await custom_fields.create_def(session, None, "C", FieldType.checkbox)
        )
        int_d = ok(
            await custom_fields.create_def(session, None, "I", FieldType.integer)
        )
        dec_d = ok(
            await custom_fields.create_def(session, None, "Dec", FieldType.decimal)
        )
        ts_d = ok(
            await custom_fields.create_def(session, None, "Ts", FieldType.timestamp)
        )
        tstz_d = ok(
            await custom_fields.create_def(session, None, "Tz", FieldType.timestamptz)
        )
        time_d = ok(await custom_fields.create_def(session, None, "Tm", FieldType.time))
        dur_d = ok(
            await custom_fields.create_def(session, None, "Dur", FieldType.interval)
        )
        uuid_d = ok(await custom_fields.create_def(session, None, "U", FieldType.uuid))

        assert ok(custom_fields.validate_value(text_d, "  hi ")) == "hi"
        assert ok(custom_fields.validate_value(text_d, "   ")) is None  # blank clears
        assert ok(custom_fields.validate_value(num_d, 5)) == 5
        assert ok(custom_fields.validate_value(num_d, 2.5)) == 2.5
        assert ok(custom_fields.validate_value(sel_d, "a")) == "a"
        assert ok(custom_fields.validate_value(date_d, "2026-07-11")) == "2026-07-11"
        assert ok(custom_fields.validate_value(check_d, True)) is True
        assert ok(custom_fields.validate_value(num_d, None)) is None
        assert ok(custom_fields.validate_value(int_d, 5)) == 5
        assert (
            ok(custom_fields.validate_value(int_d, 5.0)) == 5
        )  # ui.number hands floats
        assert ok(custom_fields.validate_value(dec_d, "1.50")) == "1.50"  # scale kept
        assert (
            ok(custom_fields.validate_value(ts_d, "2026-08-17 10:30"))
            == "2026-08-17T10:30:00"
        )
        assert (
            ok(custom_fields.validate_value(tstz_d, "2026-08-17T10:30Z"))
            == "2026-08-17T10:30:00+00:00"
        )
        assert ok(custom_fields.validate_value(time_d, "09:15")) == "09:15:00"
        assert ok(custom_fields.validate_value(dur_d, "P1DT2H")) == "P1DT2H"
        assert (
            ok(custom_fields.validate_value(dur_d, "PT90M")) == "PT1H30M"
        )  # canonical
        assert (
            ok(
                custom_fields.validate_value(
                    uuid_d, "6BA7B810-9DAD-11D1-80B4-00C04FD430C8"
                )
            )
            == "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
        )

        for defn, bad in [
            (text_d, 5),
            (num_d, "5"),
            (num_d, True),  # bool subclasses int; must be rejected
            (sel_d, "z"),
            (date_d, "tomorrow"),
            (date_d, "2026-13-40"),
            (check_d, "yes"),
            (int_d, True),
            (int_d, 2.5),
            (int_d, "5"),
            (dec_d, 2.5),  # floats taint exactness; type the digits instead
            (dec_d, "NaN"),
            (dec_d, "abc"),
            (ts_d, "2026-08-17T10:30+02:00"),  # naive type rejects offsets
            (tstz_d, "2026-08-17T10:30"),  # aware type requires an offset
            (time_d, "09:15+02:00"),
            (dur_d, "P1M"),  # months have no fixed length
            (dur_d, "3 days"),
            (uuid_d, "not-a-uuid"),
        ]:
            refused(custom_fields.validate_value(defn, bad), errors.Invalid)


async def test_set_values_merges_and_clears(database):
    async with db_session() as session:
        ok(await custom_fields.create_def(session, None, "Shirt size", FieldType.text))
        ok(await custom_fields.create_def(session, None, "Trained", FieldType.checkbox))
        v = ok(await volunteers.create(session, None, "Ada", "Lovelace"))
        vid = v.id

    async with db_session() as session:
        ok(
            await custom_fields.set_values(
                session, None, vid, {"shirt_size": "M", "trained": True}
            )
        )
    async with db_session() as session:
        ok(
            await custom_fields.set_values(session, None, vid, {"shirt_size": "L"})
        )  # merge, not replace
    async with db_session() as session:
        assert (await volunteers.get(session, vid)).custom == {
            "shirt_size": "L",
            "trained": True,
        }
        ok(
            await custom_fields.set_values(session, None, vid, {"trained": None})
        )  # None clears
    async with db_session() as session:
        assert (await volunteers.get(session, vid)).custom == {"shirt_size": "L"}

    async with db_session() as session:
        refused(
            await custom_fields.set_values(session, None, vid, {"nonexistent": "x"}),
            errors.Invalid,
        )
