"""axe-core over the main pages, in both colour modes.

What the source-reading contrast test cannot see: the page as the browser
composes it — Quasar's own classes, the inline brand colours, whatever the
cascade actually settles on — plus names, roles, landmarks and labels. A
*serious* or *critical* finding fails the test; the lesser ones are printed
so a run leaves a trail without turning every minor into a red build.

Needs a browser, which is the admission rule for this package.
"""

import pytest
from axe_playwright_python.async_playwright import Axe
from playwright.async_api import Page, expect

from volunteerdb.permissions import SYSTEM

from .conftest import icon_button, ready, sign_in
from tests import mint
from tests.conftest import db_session
from tests.fp_helpers import ok

PAGES = ("/", "/events", "/teams", "/volunteers", "/account")
# the three detail pages, once steps 10 and 35-37 of uiux-improvement.md had
# landed: a team, an event and a proposal from the seeded parish
DETAIL = ("/teams/{team}", "/events/{event}", "/elections/{proposal}")
FAIL_ON = ("moderate", "serious", "critical")


@pytest.fixture
async def detail_pages(seeded) -> list[str]:
    """The seeded parish with an event and an open proposal, so the three
    detail pages have something on them."""
    from datetime import timedelta

    import sqlalchemy as sa

    from volunteerdb.models import AppUser, TeamRole
    from volunteerdb.services import elections
    from volunteerdb.services import events as event_service

    async with db_session() as session:
        admin_id = await session.scalar(
            sa.select(AppUser.id).where(AppUser.email == "admin@example.org")
        )
        assert admin_id is not None
        starts = mint.now() + timedelta(days=7)
        created = ok(
            await event_service.create_event(
                session,
                SYSTEM,
                team_id=seeded["team_id"],
                title="Sunday Mass",
                starts_at=starts,
                ends_at=starts + timedelta(hours=2),
                created_by=None,
                tz=mint.tz(),
                series_id=mint.uuid(),
            )
        )
        today = mint.today()
        proposal = ok(
            await elections.create_proposal(
                session,
                SYSTEM,
                team_id=seeded["team_id"],
                role=TeamRole.leader,
                nomination_deadline=today + timedelta(days=5),
                voting_deadline=today + timedelta(days=15),
                created_by=admin_id,
                candidates=[elections.CandidateInput(seeded["volunteer_id"], "steady")],
                today=today,
            )
        )
        return [
            path.format(
                team=seeded["team_id"], event=created[0].id, proposal=proposal.id
            )
            for path in DETAIL
        ]


def _findings(results, impacts=FAIL_ON) -> list[str]:
    lines = []
    for v in results.response["violations"]:
        if v["impact"] not in impacts:
            continue
        targets = ", ".join(n["target"][0] for n in v["nodes"][:3])
        lines.append(f"{v['id']} [{v['impact']}] {v['help']} — {targets}")
    return lines


async def _audit(page: Page, where: str) -> None:
    results = await Axe().run(page)
    minor = _findings(results, ("minor",))
    if minor:
        print(f"{where}: lesser findings\n  " + "\n  ".join(minor))
    serious = _findings(results)
    assert not serious, f"{where}: axe-core findings\n  " + "\n  ".join(serious)


async def test_the_login_page(page: Page, base_url: str):
    await page.goto("/login")
    await ready(page)
    assert (await page.get_attribute("html", "lang") or "").startswith("en")
    await _audit(page, "/login")


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
async def test_the_signed_in_pages(
    seeded, detail_pages, page: Page, base_url: str, dark: bool
):
    await page.set_viewport_size({"width": 1280, "height": 900})
    await sign_in(page, "admin@example.org", "secret-pass-phrase")
    await ready(page)
    if dark:
        await icon_button(page, "settings").click()
        await page.get_by_role("switch", name="Dark mode").click()
        await page.keyboard.press("Escape")
        await page.wait_for_selector("body.body--dark")
    for path in (*PAGES, *detail_pages):
        await page.goto(path)
        await ready(page)
        if dark:
            await page.wait_for_selector("body.body--dark")
        assert await page.locator("main").count() == 1, f"{path}: one main landmark"
        assert await page.locator("#main").count() == 1, f"{path}: no skip target"
        assert await page.locator("a.vdb-skip").count() == 1, f"{path}: no skip link"
        await _audit(page, f"{path} ({'dark' if dark else 'light'})")


async def test_the_skip_link_is_first_and_lands_on_main(seeded, page: Page, base_url):
    await sign_in(page, "admin@example.org", "secret-pass-phrase")
    await ready(page)
    await page.keyboard.press("Tab")
    focused = await page.evaluate("document.activeElement.className")
    assert "vdb-skip" in focused, f"first Tab landed on {focused!r}, not the skip link"
    await page.keyboard.press("Enter")
    assert page.url.endswith("#main")


async def test_icon_buttons_have_names(seeded, page: Page, base_url):
    await sign_in(page, "admin@example.org", "secret-pass-phrase")
    await ready(page)
    # expect() waits for the header to be there; a plain count() is instant
    for name in ("Settings", "Your account"):
        await expect(page.get_by_role("button", name=name)).to_have_count(1)
    # and the way out, under the account menu, is a named button too
    await page.get_by_role("button", name="Your account", exact=True).click()
    await expect(page.get_by_role("button", name="Sign out")).to_have_count(1)
