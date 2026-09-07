"""Actors for tests that want a person rather than SYSTEM.

`SYSTEM` (permissions.SYSTEM) carries an admin's rights and nobody's identity,
which is what most service calls in a test want: the thing under test is
downstream of the permission check. What SYSTEM cannot do is what a person does
for themselves -- sign up, RSVP, claim a substitution, cast a ballot -- because
those check that the actor IS the volunteer. This stand-in is that volunteer,
with no account and no team rights.
"""

from collections.abc import Iterable

from volunteerdb.permissions import Actor


def as_volunteer(volunteer_id: int, *, proposals: Iterable[int] = ()) -> Actor:
    """The person behind `volunteer_id`: enough to act for themselves, and
    nothing else. `proposals` are the rolls they sit on, for cast_ballot."""
    return Actor(
        user=None,
        volunteer_id=volunteer_id,
        managed_team_ids=set(),
        people_team_ids=set(),
        full_view_team_ids=set(),
        names_view_team_ids=set(),
        voter_proposal_ids=frozenset(proposals),
    )
