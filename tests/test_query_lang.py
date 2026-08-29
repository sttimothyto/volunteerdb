"""The WHERE-filter language: detection, validation, and both compile targets.

Service-level behavior (what rows each role's query returns) lives in
test_volunteers_service.py; this file needs no data, only the parser and
the compilers.
"""

import pytest

from volunteerdb import query_lang
from volunteerdb.models import (
    AppUser,
    CustomFieldDef,
    Membership,
    Team,
    Volunteer,
)
from volunteerdb.permissions import Actor

pytestmark = pytest.mark.pure

DEFS = {
    "years_served": CustomFieldDef(key="years_served", label="Y", field_type="integer"),
    "term": CustomFieldDef(key="term", label="T", field_type="interval"),
    "trained": CustomFieldDef(key="trained", label="Tr", field_type="checkbox"),
}


def compile_v(text, actor=None):
    ast = query_lang.parse(text)
    assert ast is not None, f"expected a query: {text!r}"
    return query_lang.compile_volunteers(
        ast, V=Volunteer, M=Membership, T=Team, defs=DEFS, actor=actor
    )


def test_detection_matrix():
    substring = [
        "",
        "maria",
        "Rob and Ann",  # bare names are not comparisons
        "Rob or Ann",
        "O'Brien",  # unterminated quote tokenizes as an error
        "phone LIKE '5",
        "not sure yet",
        "Maria in Liturgy",
        "x = 1; DROP TABLE volunteer",  # trailing tokens kill the parse
        "x = 1 GROUP BY y",
        "name",  # a bare field is not a condition (write name = '…')
        "true",
    ]
    for text in substring:
        assert query_lang.parse(text) is None, text

    queries = [
        "name ILIKE 'a%'",
        "phone LIKE '555%' AND team = 'Liturgy'",
        "email LIKE '%@gmail.com' OR phone LIKE '555%'",
        "NOT (notes IS NULL)",
        "role IN ('leader', 'second')",
        "custom.years_served BETWEEN 1 AND 5",
        "id != 4",
        "5 < id",
        "trained = true",
        "created > '2025-01-01'",
    ]
    for text in queries:
        assert query_lang.parse(text) is not None, text


def test_compile_rejections_name_the_problem():
    cases = [
        ("bogus = 1", "unknown field: bogus"),
        ("lower(name) = 'a'", "must be a field"),
        ("name = phone", "two fields"),
        ("id IN (SELECT 1)", "subqueries"),
        ("role = 'boss'", "role must be one of"),
        ("custom.years_served LIKE 'x'", "LIKE works on text"),
        ("is_active > true", "true/false fields"),
        ("custom.nonexistent = 1", "unknown field"),
        ("app_user.email = 'x'", "unknown qualifier"),
        ("id = 'five'", "without quotes"),
        ("name = 5", "must be quoted"),
        ("created > 'not a date'", "created"),
        ("custom.term > 'P1M'", "ISO 8601 duration"),
        ("name > name", "two fields"),
    ]
    for text, needle in cases:
        with pytest.raises(query_lang.QueryError, match=needle):
            compile_v(text)


def test_unknown_field_error_lists_the_fields():
    with pytest.raises(query_lang.QueryError) as err:
        compile_v("bogus = 1")
    message = str(err.value)
    assert "name" in message and "custom.years_served" in message
    assert "password" not in message


def test_private_and_roster_leaves_are_scoped_for_members():
    actor = Actor(
        user=AppUser(is_admin=False),
        volunteer_id=7,
        managed_team_ids=set(),
        people_team_ids=set(),
        full_view_team_ids=set(),
        names_view_team_ids={3},
    )
    sql = str(compile_v("phone LIKE '555%'", actor))
    assert "volunteer.id =" in sql, "private leaf is AND-ed with the visibility scope"
    sql = str(compile_v("email LIKE 'j%'", actor))
    assert "volunteer.id =" in sql, (
        "email is private too: unwrapped, this leaf walked an address out of a "
        "column the list renders as `•••`"
    )
    sql = str(compile_v("NOT (phone LIKE '555%')", actor))
    assert "volunteer.id =" in sql and "NOT LIKE" in sql, (
        "negation lands on the comparison, never on the visibility scope"
    )
    sql = str(compile_v("team = 'X'", actor))
    assert "team_id IN" in sql, "roster EXISTS is limited to visible rosters"
    sql = str(compile_v("team != 'X'", actor))
    assert "NOT (EXISTS" in sql, "!= negates the EXISTS, not the name match"

    admin = Actor(
        user=AppUser(is_admin=True),
        volunteer_id=None,
        managed_team_ids=set(),
        people_team_ids=set(),
        full_view_team_ids=set(),
        names_view_team_ids=set(),
    )
    sql = str(compile_v("phone LIKE '555%'", admin))
    assert "volunteer.id" not in sql, "admins skip the wrap"


ROWS = [
    {
        "name": "Liturgy",
        "path": "Liturgy",
        "depth": 0,
        "inactive": False,
        "description": "Sunday services",
        "leader": 1,
        "second": 0,
        "core": 2,
        "member": 5,
        "total": 8,
        "gaps": 1,
    },
    {
        # counts blanked: the viewer does not manage this team
        "name": "Music",
        "path": "Liturgy > Music",
        "depth": 1,
        "inactive": True,
        "description": "",
        "leader": "",
        "second": "",
        "core": "",
        "member": "",
        "total": "",
        "gaps": "",
    },
]


def compile_t(text):
    ast = query_lang.parse(text)
    assert ast is not None, f"expected a query: {text!r}"
    return query_lang.compile_teams(ast)


def test_teams_backend_semantics():
    cases = [
        ("total > 3", [True, False]),
        ("NOT (total > 3)", [False, False]),  # blanked counts stay false under NOT
        ("total IS NULL", [False, True]),
        ("name ILIKE 'm%'", [False, True]),
        ("name LIKE 'm%'", [False, False]),  # LIKE is case-sensitive
        ("path LIKE '%Music'", [False, True]),
        ("path LIKE '%usic'", [False, True]),  # % crosses the > separator too
        ("name LIKE 'L_turgy'", [True, False]),
        ("is_active = true", [True, False]),
        ("is_active != true", [False, True]),
        ("description IS NULL", [False, True]),  # empty description reads as NULL
        ("gaps = 1 AND is_active = true", [True, False]),
        ("name = 'Liturgy' OR name = 'Music'", [True, True]),
        ("NOT (name = 'Liturgy' OR name = 'Music')", [False, False]),
        ("leaders >= 1", [True, False]),
        ("members BETWEEN 2 AND 9", [True, False]),
        ("name IN ('Music', 'Choir')", [False, True]),
        ("name NOT IN ('Music', 'Choir')", [True, False]),
    ]
    for text, expected in cases:
        pred = compile_t(text)
        assert [pred(r) for r in ROWS] == expected, text


def test_teams_backend_rejects_unknown_fields():
    with pytest.raises(query_lang.QueryError, match="unknown field: phone"):
        compile_t("phone = 'x'")
    with pytest.raises(query_lang.QueryError, match="unknown field"):
        compile_t("custom.years_served = 1")


EVENT_ROWS = [
    {
        "id": 1,
        "when": "2026-09-06 10:30",
        "title": "Sunday Mass",
        "team": "Liturgy",
        "location": "Main church",
        "filled": "2/3",
        "filled_n": 2,
        "capacity_n": 3,
        "you": "serving",
    },
    {
        "id": 2,
        "when": "2026-12-24 19:00",
        "title": "Carol night",
        "team": "Liturgy > Music",
        "location": "",
        "filled": "0/∞",
        "filled_n": 0,
        "capacity_n": None,  # unlimited slots have no capacity
        "you": "",
    },
]


def compile_e(text):
    ast = query_lang.parse(text)
    assert ast is not None, f"expected a query: {text!r}"
    return query_lang.compile_events(ast)


def test_events_backend_semantics():
    cases = [
        ("title ILIKE '%mass%'", [True, False]),
        ("date = '2026-09-06'", [True, False]),
        ("date >= '2026-10-01'", [False, True]),
        ("team LIKE '%Music'", [False, True]),
        ("location IS NULL", [False, True]),  # empty location reads as NULL
        ("filled = 0", [False, True]),
        ("capacity IS NULL", [False, True]),  # None never matches a comparison…
        ("capacity > 1", [True, False]),  # …and stays out of ranges too
        ("you = 'serving'", [True, False]),
        ("filled < 3 AND date < '2026-10-01'", [True, False]),
    ]
    for text, expected in cases:
        pred = compile_e(text)
        assert [pred(r) for r in EVENT_ROWS] == expected, text


def test_events_backend_rejects_unknown_fields():
    with pytest.raises(query_lang.QueryError, match="unknown field: slot"):
        compile_e("slot = 'x'")
