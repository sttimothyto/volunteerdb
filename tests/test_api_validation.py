"""What the API refuses before any service runs: malformed JSON, the wrong
type, a missing field, an enum value that does not exist, an address that
is not one, a date that is not a date, and every length and range a schema
declares. Each is a 422 with a detail that names the field -- and one row
pins that a field nobody declared is silently ignored, which is the
behaviour today (no input model sets extra="forbid")."""

import pytest

from tests.conftest import _token

LONG = "x" * 501


async def _admin(client):
    return await _token(client, "admin@example.org", "secret-pass-phrase")


@pytest.mark.parametrize(
    ("method", "path", "body", "field"),
    [
        # min_length / max_length
        (
            "POST",
            "/api/volunteers",
            {"first_name": "", "last_name": "Able"},
            "first_name",
        ),
        (
            "POST",
            "/api/volunteers",
            {"first_name": "A", "last_name": "x" * 101},
            "last_name",
        ),
        ("POST", "/api/teams", {"name": ""}, "name"),
        ("POST", "/api/teams", {"name": "x" * 201}, "name"),
        (
            "POST",
            "/api/custom-fields",
            {"key": "shirt", "label": "", "field_type": "text"},
            "label",
        ),
        ("PATCH", "/api/teams/{team_id}/home-doc", {"url": LONG}, "url"),
        ("PATCH", "/api/teams/{team_id}/roster-sheet", {"url": LONG}, "url"),
        # ge
        (
            "POST",
            "/api/teams",
            {"name": "Choir", "workload_weight": -1},
            "workload_weight",
        ),
        ("PATCH", "/api/teams/{team_id}", {"workload_weight": -0.5}, "workload_weight"),
        # enums
        (
            "POST",
            "/api/memberships",
            {"volunteer_id": 1, "team_id": 1, "role": "bishop"},
            "role",
        ),
        (
            "POST",
            "/api/teams/{team_id}/roster-sheet/sync",
            {"direction": "sideways"},
            "direction",
        ),
        # EmailStr
        ("POST", "/api/users", {"email": "not-an-address"}, "email"),
        ("POST", "/api/auth/email-change", {"new_email": "nope"}, "new_email"),
        # types and shapes
        (
            "POST",
            "/api/volunteers",
            {"first_name": ["Ann"], "last_name": "Able"},
            "first_name",
        ),
        (
            "POST",
            "/api/teams",
            {"name": "Choir", "workload_weight": "heavy"},
            "workload_weight",
        ),
        ("POST", "/api/volunteers", {"last_name": "Able"}, "first_name"),  # required
        (
            "POST",
            "/api/elections/proposals",
            {
                "team_id": 1,
                "role": "second",
                "nomination_deadline": "2026-09-01",
                "voting_deadline": "2026-09-08",
                "candidates": [],
            },
            "candidates",
        ),
        (
            "POST",
            "/api/events",
            {
                "team_id": 1,
                "title": "Mass",
                "starts_at": "yesterday",
                "ends_at": "2026-09-01T11:00:00Z",
            },
            "starts_at",
        ),
        (
            "POST",
            "/api/events",
            {
                "team_id": 1,
                "title": "",
                "starts_at": "2026-09-01T10:00:00Z",
                "ends_at": "2026-09-01T11:00:00Z",
            },
            "title",
        ),
        (
            "POST",
            "/api/events",
            {
                "team_id": 1,
                "title": "Mass",
                "starts_at": "2026-09-01T10:00:00Z",
                "ends_at": "2026-09-01T11:00:00Z",
                "slots": [{"name": "Lector", "capacity": 0}],
            },
            "capacity",
        ),
        (
            "PUT",
            "/api/auth/password",
            {"current_password": "x", "new_password": ""},
            "new_password",
        ),
        ("POST", "/api/auth/email-change/confirm", {"token": ""}, "token"),
        (
            "POST",
            "/api/auth/redeem-invite",
            {"token": "", "agreed_to_confidentiality": True},
            "token",
        ),
    ],
)
async def test_the_schema_refuses_it_and_names_the_field(
    client, seeded, method, path, body, field
):
    headers = await _admin(client)
    url = path.format(team_id=seeded["team_id"])
    r = await client.request(method, url, json=body, headers=headers)
    assert r.status_code == 422, r.text
    locations = [tuple(e["loc"]) for e in r.json()["detail"]]
    assert any(field in loc for loc in locations), (field, r.json())


async def test_a_body_that_is_not_json_is_a_422_not_a_500(client, seeded):
    headers = {**await _admin(client), "Content-Type": "application/json"}
    r = await client.post("/api/teams", content=b"{not json", headers=headers)
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "json_invalid"


async def test_an_unknown_field_is_ignored_and_changes_nothing(client, seeded):
    """Pinned as it is, not as it should be: no input model sets
    extra="forbid", so a typo'd key returns 200 having changed nothing. A
    client that misspells first_name learns that from this test, not from
    a 422. Flip this test when the schemas start refusing extras."""
    headers = await _admin(client)
    vid = seeded["volunteer_id"]
    r = await client.patch(
        f"/api/volunteers/{vid}", json={"frist_name": "Typo"}, headers=headers
    )
    assert r.status_code == 200
    assert r.json()["first_name"] == "Maria", "nothing changed"
