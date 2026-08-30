"""The app's guide links: every tier gets its groups, the sign-in page gets the
pages a reader with no account can use, and every link lands on a page that
exists in the user guide."""

import pathlib

import pytest

from volunteerdb.models import AppUser
from volunteerdb.permissions import Actor
from volunteerdb.ui import help_links

pytestmark = pytest.mark.pure

GUIDE = pathlib.Path(__file__).resolve().parent.parent / "docs" / "guide"


def _actor(
    *,
    admin: bool = False,
    volunteer_id: int | None = 7,
    managed: set[int] | None = None,
    full_view: set[int] | None = None,
    voter: frozenset[int] = frozenset(),
) -> Actor:
    managed = managed or set()
    return Actor(
        user=AppUser(email="a@example.org", is_admin=admin),
        volunteer_id=volunteer_id,
        managed_team_ids=managed,
        people_team_ids=set(managed),
        full_view_team_ids=(full_view or set()) | managed,
        names_view_team_ids=set(),
        voter_proposal_ids=voter,
    )


def titles(actor: Actor) -> list[str]:
    return [group.title for group in help_links.groups_for(actor)]


def test_the_groups_accumulate_with_reach():
    assert titles(_actor()) == ["For everyone", "For team members"]
    assert titles(_actor(voter=frozenset({1}))) == [
        "For everyone",
        "For team members",
        "For voters",
    ]
    assert titles(_actor(full_view={1})) == [
        "For everyone",
        "For team members",
        "For core members",
    ]
    assert titles(_actor(managed={1})) == [
        "For everyone",
        "For team members",
        "For core members",
        "For leaders and seconds",
    ]
    # an admin nobody linked to a volunteer record has no teams, shifts or photo
    assert titles(_actor(admin=True, volunteer_id=None)) == [
        "For everyone",
        "For core members",
        "For leaders and seconds",
        "For administrators",
    ]


def test_every_link_lands_on_a_page_of_the_guide():
    missing = []
    for group in (
        help_links.SIGNING_IN,
        help_links.EVERYONE,
        help_links.MEMBERS,
        help_links.VOTERS,
        help_links.CORE,
        help_links.LEADERS,
        help_links.ADMINS,
    ):
        for link in group.links:
            assert link.href.startswith("/manual/"), link
            if link.path.startswith("/"):
                continue  # the technical manual's own landing page
            source = GUIDE / link.path.replace(".html", ".md")
            if not source.is_file():
                missing.append(str(source))
    assert not missing, f"guide links with no page behind them: {missing}"


def test_the_sign_in_page_offers_only_pages_a_signed_out_reader_can_use():
    """Under the help icon at the door (ui/login.py). Every page there is
    public — the guide is (main.PUBLIC_MANUAL_PREFIXES) — and none of them
    asks the reader to already be signed in, which is the one thing they
    cannot do."""
    paths = [link.path for link in help_links.SIGNING_IN.links]
    assert "tutorials/first-sign-in.html" in paths, (
        "the page for the reader holding an invitation is the point of the icon"
    )
    assert "how-to/sign-in-with-a-code.html" in paths, (
        "the way in for a reader with no password is the first thing to offer"
    )
    assert help_links.GUIDE_HOME in paths, "and the way into the rest of the guide"

    for link in help_links.SIGNING_IN.links:
        public = link.href == help_links.GUIDE_HOME or link.href.startswith(
            help_links.GUIDE
        )
        assert public, (
            f"{link.href} is outside the public half of the manual — a reader "
            "who cannot sign in would be bounced to the page they came from"
        )
        if link.path.startswith("/"):
            continue
        page = (GUIDE / link.path.replace(".html", ".md")).read_text()
        assert "- You are signed in." not in page, (
            f"{link.path} starts by asking the reader to sign in, which is what "
            "they came to this icon unable to do"
        )


def test_titles_match_the_pages_they_open():
    """The dashboard says what the page is called, so the reader recognises
    it when it opens."""
    wrong = []
    for group in [help_links.SIGNING_IN, *help_links.groups_for(_actor(admin=True))]:
        for link in group.links:
            if link.path.startswith("/"):
                continue
            source = GUIDE / link.path.replace(".html", ".md")
            heading = source.read_text().splitlines()[0].removeprefix("# ").strip()
            if heading != link.title:
                wrong.append(
                    f"{link.path}: dashboard says {link.title!r}, page says {heading!r}"
                )
    assert not wrong, wrong
