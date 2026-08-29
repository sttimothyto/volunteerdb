"""Reader-chosen column order: the permutation logic and the wire contract.

A real drag cannot be tested here — the UI suite is NiceGUI's headless
simulation, which has no DOM and no pointer. What is testable is everything on
both sides of the gesture: the pure reorder/merge logic below, and the
vdbColMove event the browser sends, triggered directly on the table element.
That leaves exactly one untested link (mousedown-move-drop -> $emit), which is
the part to check by hand in a browser.
"""

from collections import Counter

import pytest
from nicegui import app, ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.db import db_session
from volunteerdb.models import FieldType
from volunteerdb.services import custom_fields as custom_field_service
from volunteerdb.ui import column_order
from volunteerdb.ui.context import clear_session

from .test_ui_teams import SIM_MAIN, _parish, _table
from tests.fp_helpers import ok

TEAMS_DEFAULT = ["team", "leader", "second", "core", "member", "total", "gaps"]


def _drag(user, moved: str, target: str) -> None:
    """What the browser sends when a header is dropped on another one."""
    user.find(kind=ui.table).trigger("vdbColMove", {"moved": moved, "target": target})


# --- reorder ------------------------------------------------------------------


@pytest.mark.parametrize(
    "moved,target,expected",
    [
        # rightwards: lands after the target
        ("a", "c", ["b", "c", "a", "d"]),
        # leftwards: lands before it
        ("d", "b", ["a", "d", "b", "c"]),
        # adjacent is a swap either way
        ("a", "b", ["b", "a", "c", "d"]),
        ("b", "a", ["b", "a", "c", "d"]),
    ],
)
def test_reorder_gives_the_moved_column_the_targets_slot(moved, target, expected):
    assert column_order.reorder(["a", "b", "c", "d"], moved, target) == expected


@pytest.mark.parametrize(
    "order,moved,target",
    [
        (["a", "b"], "a", "a"),  # dropped on itself
        (["a", "b"], "zz", "a"),  # a column this table does not have
        (["a", "b"], "a", "zz"),
        ([], "a", "b"),  # nothing to reorder
    ],
)
def test_reorder_ignores_what_it_does_not_recognise(order, moved, target):
    assert column_order.reorder(order, moved, target) == order, (
        "both arguments arrive over the socket; anything unrecognised must be "
        "returned unchanged rather than raising or dropping a column"
    )


# --- merge --------------------------------------------------------------------


def test_merge_with_nothing_saved_is_the_page_order():
    present = ["name", "email", "status"]
    assert column_order.merge([], present) == present, (
        "the identity case is what keeps every page's declared default order — "
        "test_ui_teams.test_every_column_sorts asserts that exact list"
    )


def test_merge_places_a_column_the_save_never_saw_beside_its_neighbour():
    # cf_diocese was added since the drag; it must appear, next to the column
    # the page declared it after
    saved = ["status", "name", "email"]
    present = ["name", "email", "cf_diocese", "status"]
    assert column_order.merge(saved, present) == [
        "status",
        "name",
        "email",
        "cf_diocese",
    ]


def test_merge_puts_a_new_leading_column_first():
    assert column_order.merge(["status", "name"], ["cf_a", "name", "status"]) == [
        "cf_a",
        "status",
        "name",
    ]


def test_merge_never_resurrects_a_column_that_is_gone():
    saved = ["status", "name", "email", "leader"]
    assert column_order.merge(saved, ["name", "email", "status"]) == [
        "status",
        "name",
        "email",
    ]


@pytest.mark.parametrize(
    "saved,present",
    [
        (["b", "a"], ["a", "b", "c"]),
        (["a", "a", "b"], ["a", "b"]),  # duplicates in the saved list
        (["x", "y"], ["a", "b"]),  # disjoint
        ([], []),
        (["c", "b", "a"], ["a", "b", "c"]),
    ],
)
def test_merge_is_always_a_permutation(saved, present):
    assert Counter(column_order.merge(saved, present)) == Counter(present), (
        "merge sorts `present`, so it can never lose or duplicate a column; a "
        "regression here silently deletes a column from the page"
    )


# --- apply_order --------------------------------------------------------------


def _cols(*names):
    return [{"name": n, "label": n.title()} for n in names]


def test_apply_order_holds_a_fixed_column_in_place():
    columns = _cols("team", "leader", "gaps")
    columns[0][column_order.FIXED] = True

    # `team` is named last in the saved order and still comes first
    named_last = column_order.apply_order(["leader", "gaps", "team"], columns)
    assert [c["name"] for c in named_last] == ["team", "leader", "gaps"]

    omitted = column_order.apply_order(["gaps", "leader"], columns)
    assert [c["name"] for c in omitted] == ["team", "gaps", "leader"]


def test_apply_order_keeps_the_column_dicts_untouched():
    columns = _cols("a", "b")
    out = column_order.apply_order(["b", "a"], columns)
    assert out == [columns[1], columns[0]], "the same dicts, in a different order"


# --- the wire, end to end through the simulation ------------------------------


async def test_a_drop_reorders_the_table_and_the_order_survives(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['admin_u']}")
        await user.open("/teams")
        assert [c["name"] for c in _table(user).columns] == TEAMS_DEFAULT

        _drag(user, "gaps", "leader")
        assert [c["name"] for c in _table(user).columns] == [
            "team",
            "gaps",
            "leader",
            "second",
            "core",
            "member",
            "total",
        ], "the drop moves the column there and then, without a reload"

        await user.open("/teams")
        assert [c["name"] for c in _table(user).columns][:3] == [
            "team",
            "gaps",
            "leader",
        ], "and it survives a reload"

        await user.open("/volunteers")
        await user.open("/teams")
        assert [c["name"] for c in _table(user).columns][:3] == [
            "team",
            "gaps",
            "leader",
        ], "and navigating away and back"


async def test_the_pinned_team_column_cannot_be_dragged_away(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['admin_u']}")
        await user.open("/teams")
        # the wire is untrusted: the pin has to hold server-side, not only in CSS
        _drag(user, "team", "total")
        assert [c["name"] for c in _table(user).columns] == TEAMS_DEFAULT


async def test_a_junk_payload_is_ignored(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['admin_u']}")
        await user.open("/teams")
        for payload in ({}, {"moved": "gaps"}, {"moved": 1, "target": 2}):
            user.find(kind=ui.table).trigger("vdbColMove", payload)
        assert [c["name"] for c in _table(user).columns] == TEAMS_DEFAULT


async def test_a_custom_field_added_since_the_drag_still_appears(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['admin_u']}")
        await user.open("/volunteers")
        _drag(user, "status", "name")
        assert [c["name"] for c in _table(user).columns][0] == "status"

        async with db_session() as session:
            ok(
                await custom_field_service.create_def(
                    session, None, "Diocese", FieldType.text, show_in_list=True
                )
            )

        await user.open("/volunteers")
        names = [c["name"] for c in _table(user).columns]
        assert names[0] == "status", "the saved order still leads"
        assert "cf_diocese" in names, (
            "a column added since the drag must still appear — a saved order is "
            "never allowed to hide one"
        )


async def test_a_plain_member_sees_the_one_column_listing(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['mia_u']}")
        await user.open("/teams")
        assert [c["name"] for c in _table(user).columns] == ["team"]
        # nothing movable at all: the only column is the pinned one
        _drag(user, "team", "team")
        assert [c["name"] for c in _table(user).columns] == ["team"]


async def test_signing_out_forgets_the_order_but_keeps_dark_mode(database):
    """The lifetime contract: the order is scoped to the sitting, the dark-mode
    pref is scoped to the browser."""
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['admin_u']}")
        await user.open("/teams")
        _drag(user, "gaps", "leader")

        with user.client:
            app.storage.user["dark_mode"] = True
            assert column_order.STORAGE_KEY in app.storage.user, "the drag was saved"
            clear_session()
            assert column_order.STORAGE_KEY not in app.storage.user
            assert app.storage.user.get("dark_mode") is True, (
                "dark mode is how this browser likes to read, whoever is signed in"
            )


async def test_the_header_cell_slot_is_wired_on_both_listings(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['admin_u']}")
        for path in ("/teams", "/volunteers"):
            await user.open(path)
            table = user.find(kind=ui.table).elements.pop()
            assert "header-cell" in table.slots, f"{path} has no draggable header"
            assert "data-vdb-col" in table.slots["header-cell"].template
