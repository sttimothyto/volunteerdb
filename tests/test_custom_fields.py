"""Custom field definitions and per-volunteer values (Volunteer.custom JSONB)."""

import pytest
from sqlalchemy.exc import IntegrityError

from volunteerdb.db import db_session
from volunteerdb.models import FieldType
from volunteerdb.services import custom_fields, volunteers


async def test_create_def_slugifies_and_orders(database):
    async with db_session() as session:
        await custom_fields.create_def(
            session, "Safeguarding training", FieldType.date, position=2
        )
        await custom_fields.create_def(
            session, "Shirt size!", FieldType.text, position=1
        )

    async with db_session() as session:
        defs = await custom_fields.list_defs(session)
        assert [d.key for d in defs] == ["shirt_size", "safeguarding_training"]
        assert defs[0].label == "Shirt size!"


async def test_duplicate_key_conflicts(database):
    async with db_session() as session:
        await custom_fields.create_def(session, "Shirt size", FieldType.text)
    with pytest.raises(IntegrityError):
        async with db_session() as session:
            await custom_fields.create_def(session, "shirt SIZE?", FieldType.number)


async def test_select_requires_options(database):
    with pytest.raises(ValueError):
        async with db_session() as session:
            await custom_fields.create_def(
                session, "Preferred contact", FieldType.select
            )
    with pytest.raises(ValueError):
        async with db_session() as session:
            await custom_fields.create_def(
                session, "Preferred contact", FieldType.select, options=["", "  "]
            )


async def test_update_def_replaces_options_and_deactivates(database):
    async with db_session() as session:
        defn = await custom_fields.create_def(
            session, "Preferred contact", FieldType.select, options=["Email", "Phone"]
        )
        fid = defn.id

    async with db_session() as session:
        await custom_fields.update_def(
            session, fid, label="Contact preference", options=["Email", "Phone", "Post"]
        )
        await custom_fields.update_def(session, fid, is_active=False)

    async with db_session() as session:
        assert await custom_fields.list_defs(session) == []
        (defn,) = await custom_fields.list_defs(session, include_inactive=True)
        assert defn.label == "Contact preference"
        assert defn.options == ["Email", "Phone", "Post"]
        assert defn.key == "preferred_contact"  # key is immutable

    with pytest.raises(ValueError):  # options only make sense on select fields
        async with db_session() as session:
            text_def = await custom_fields.create_def(
                session, "Notes 2", FieldType.text
            )
            await custom_fields.update_def(session, text_def.id, options=["a"])


async def test_validate_value_matrix(database):
    async with db_session() as session:
        text_d = await custom_fields.create_def(session, "T", FieldType.text)
        num_d = await custom_fields.create_def(session, "N", FieldType.number)
        sel_d = await custom_fields.create_def(
            session, "S", FieldType.select, options=["a", "b"]
        )
        date_d = await custom_fields.create_def(session, "D", FieldType.date)
        check_d = await custom_fields.create_def(session, "C", FieldType.checkbox)

        assert custom_fields.validate_value(text_d, "  hi ") == "hi"
        assert custom_fields.validate_value(text_d, "   ") is None  # blank clears
        assert custom_fields.validate_value(num_d, 5) == 5
        assert custom_fields.validate_value(num_d, 2.5) == 2.5
        assert custom_fields.validate_value(sel_d, "a") == "a"
        assert custom_fields.validate_value(date_d, "2026-07-11") == "2026-07-11"
        assert custom_fields.validate_value(check_d, True) is True
        assert custom_fields.validate_value(num_d, None) is None

        for defn, bad in [
            (text_d, 5),
            (num_d, "5"),
            (num_d, True),  # bool subclasses int; must be rejected
            (sel_d, "z"),
            (date_d, "tomorrow"),
            (date_d, "2026-13-40"),
            (check_d, "yes"),
        ]:
            with pytest.raises(ValueError):
                custom_fields.validate_value(defn, bad)


async def test_set_values_merges_and_clears(database):
    async with db_session() as session:
        await custom_fields.create_def(session, "Shirt size", FieldType.text)
        await custom_fields.create_def(session, "Trained", FieldType.checkbox)
        v = await volunteers.create(session, "Ada", "Lovelace")
        vid = v.id

    async with db_session() as session:
        await custom_fields.set_values(
            session, vid, {"shirt_size": "M", "trained": True}
        )
    async with db_session() as session:
        await custom_fields.set_values(
            session, vid, {"shirt_size": "L"}
        )  # merge, not replace
    async with db_session() as session:
        assert (await volunteers.get(session, vid)).custom == {
            "shirt_size": "L",
            "trained": True,
        }
        await custom_fields.set_values(session, vid, {"trained": None})  # None clears
    async with db_session() as session:
        assert (await volunteers.get(session, vid)).custom == {"shirt_size": "L"}

    with pytest.raises(ValueError):
        async with db_session() as session:
            await custom_fields.set_values(session, vid, {"nonexistent": "x"})
