"""The one rule for writing an address onto a volunteer's record, as values:
both doors consult volunteers.address_change, so this is where the answers
are pinned (docs/explanation/auth.md, "changing an address")."""

import pytest

from volunteerdb.models import AppUser, Volunteer
from volunteerdb.permissions import SYSTEM, Actor
from volunteerdb.services.volunteers import AddressChange, address_change

pytestmark = pytest.mark.pure


def _actor(volunteer_id: int | None, login: str = "me@example.org") -> Actor:
    return Actor(
        user=AppUser(id=1, email=login, is_admin=False),
        volunteer_id=volunteer_id,
        managed_team_ids=set(),
        people_team_ids=set(),
        full_view_team_ids=set(),
        names_view_team_ids=set(),
    )


def _volunteer(email: str | None) -> Volunteer:
    return Volunteer(id=7, first_name="Maria", last_name="Alvarez", email=email)


@pytest.mark.parametrize(
    ("actor", "on_file", "typed", "expected"),
    [
        # somebody else's record: an ordinary edit, whatever is typed
        (_actor(3), "old@example.org", "new@example.org", AddressChange.plain),
        (_actor(3), "old@example.org", "", AddressChange.plain),
        (SYSTEM, "old@example.org", "new@example.org", AddressChange.plain),
        # your own record
        (_actor(7), "me@example.org", " Me@Example.org ", AddressChange.unchanged),
        (_actor(7), None, "me@example.org", AddressChange.sync_login),
        (_actor(7), "old@example.org", "ME@example.org", AddressChange.sync_login),
        (
            _actor(7),
            "old@example.org",
            "new@example.org",
            AddressChange.needs_confirmation,
        ),
        (_actor(7), "old@example.org", "", AddressChange.blank_own),
        (_actor(7), None, None, AddressChange.blank_own),
    ],
)
def test_address_change(actor, on_file, typed, expected):
    assert address_change(actor, _volunteer(on_file), typed) is expected
