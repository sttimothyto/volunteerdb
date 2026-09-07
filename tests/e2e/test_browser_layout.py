"""The header nav, decided by the CSS cascade rather than by the element tree.

tests/test_ui_css_invariants.py guards the mistake that produced this layout —
Tailwind's `hidden md:flex`, which Quasar's `.hidden{display:none!important}`
turns into "invisible at every width" — and can only do it by reading the
source: "Nothing in a headless render catches this — the element and its
classes are present and correct in the element tree; only the browser cascade
hides it."

So this is the other half. It asserts nothing about classes; it asks the
browser which of the two navs a reader can actually see, at a desktop width and
at a phone width, and whether the one that is showing works.
"""

import re

from playwright.async_api import expect

from volunteerdb.models import TeamRole
from volunteerdb.permissions import SYSTEM
from volunteerdb.services import memberships, teams

from .conftest import icon_button, ready, sign_in
from tests.conftest import db_session
from tests.fp_helpers import ok

DESKTOP = {"width": 1280, "height": 900}
# Below Quasar's md breakpoint (1024px), which is where gt-sm stops and lt-md
# starts — a 10" tablet in portrait, or any phone.
NARROW = {"width": 820, "height": 900}


async def test_the_header_nav_becomes_a_menu_on_a_narrow_window(seeded, page):
    await page.set_viewport_size(DESKTOP)
    await sign_in(page, "admin@example.org", "secret-pass-phrase")
    await ready(page)
    nav_link = page.locator("header").get_by_role("link", name="Volunteers", exact=True)
    menu_button = icon_button(page, "menu")

    # wide: the row of links is the nav, and the menu button stays out of it
    await expect(nav_link).to_be_visible()
    await expect(menu_button).to_be_hidden()

    # narrow: they trade places (Quasar recomputes its breakpoint on resize)
    await page.set_viewport_size(NARROW)
    await expect(nav_link).to_be_hidden()
    await expect(menu_button).to_be_visible()

    # and the menu is a nav, not just an icon: it opens, and it goes somewhere
    await menu_button.click()
    # The menu renders in a portal at the end of <body>, not inside the header,
    # and Quasar gives its items role="listitem" — so they are anchors that no
    # amount of get_by_role("link") will find.
    await page.locator('.q-menu a[href="/volunteers"]').click()
    await expect(page).to_have_url(re.compile(r"/volunteers$"))
    await expect(page.get_by_text("1 volunteer")).to_be_visible()


async def test_the_page_never_scrolls_sideways_on_a_phone(seeded, page):
    """A header built from a row of buttons is the classic way to give a page a
    horizontal scrollbar on a phone — the row keeps its width, the window does
    not, and every page inherits the overflow from the frame."""
    await page.set_viewport_size({"width": 390, "height": 844})  # a small phone
    await sign_in(page, "admin@example.org", "secret-pass-phrase")
    await ready(page)

    for path in ("/", "/volunteers", "/teams"):
        await page.goto(path)
        await ready(page)
        overflow = await page.evaluate(
            "() => document.documentElement.scrollWidth - "
            "document.documentElement.clientWidth"
        )
        assert overflow <= 0, f"{path} scrolls {overflow}px sideways at 390px wide"


PHONE = {"width": 360, "height": 780}  # the commonest Android width


async def test_a_phone_gets_one_header_row_paired_tiles_and_a_panel_that_fits(
    seeded, page
):
    """Three more decisions the cascade makes at a phone width (theme.css,
    the narrow-screens block), each of which rendered a correct element tree
    while looking wrong:

    * the header — NiceGUI's `wrap` folded the gear and sign-out onto a
      second row, so every page started under a header twice its height;
    * the dashboard tiles — a wrapping row put one tile per line down the
      whole page when two fit side by side;
    * the volunteer side panel — a 380px drawer on a 360px screen hung its
      first 20px off the left edge, badge and all.
    """
    await page.set_viewport_size(PHONE)
    await sign_in(page, "admin@example.org", "secret-pass-phrase")
    await ready(page)

    # one row: the settings gear sits level with the brand, not under it
    brand = await page.locator("header").get_by_text("Dashboard").bounding_box()
    gear = await icon_button(page, "settings").bounding_box()
    assert brand and gear
    assert abs(brand["y"] - gear["y"]) < brand["height"], (
        f"the header wrapped: brand at y={brand['y']:.0f}, the gear at y={gear['y']:.0f}"
    )

    # two abreast: the first two tiles share a row and neither leaves the screen
    tiles = page.locator(".vdb-stat")
    first, second = (
        await tiles.nth(0).bounding_box(),
        await tiles.nth(1).bounding_box(),
    )
    assert first and second
    assert abs(first["y"] - second["y"]) < 1, "the dashboard tiles stacked one per row"
    assert second["x"] + second["width"] <= PHONE["width"], "the second tile is cut off"

    # the panel fits: its left edge is on the screen, so is its close button
    await page.goto(f"/teams/{seeded['team_id']}")
    await ready(page)
    await page.get_by_text("Maria Alvarez", exact=True).first.click()
    drawer = page.locator(".q-drawer")
    await expect(drawer).to_be_visible()
    box = await drawer.bounding_box()
    assert box and box["x"] >= 0, f"the side panel starts {-box['x']:.0f}px off-screen"
    await expect(icon_button(page, "close")).to_be_in_viewport()


async def test_a_phone_sees_the_columns_it_can_show(seeded, page):
    """theme.css hides .vdb-col-wide below 40rem and grows the first cell's
    second line: at 390px the events table shows When, Event and Filled,
    and the volunteers table carries the address under the name."""
    await page.set_viewport_size({"width": 390, "height": 844})
    await sign_in(page, "admin@example.org", "secret-pass-phrase")
    await ready(page)

    await page.goto("/volunteers")
    await ready(page)
    headers = page.locator("thead th")
    await expect(headers.filter(has_text="Name")).to_be_visible()
    await expect(headers.filter(has_text="Email")).to_be_hidden()
    await expect(headers.filter(has_text="Phone")).to_be_hidden()
    # the address moved under the name
    await expect(
        page.locator(".vdb-phone-only", has_text="maria@example.org")
    ).to_be_visible()

    await page.set_viewport_size({"width": 1280, "height": 900})
    await expect(headers.filter(has_text="Email")).to_be_visible()
    await expect(
        page.locator(".vdb-phone-only", has_text="maria@example.org")
    ).to_be_hidden()


async def test_a_finger_gets_44px_targets(seeded, browser, base_url):
    """theme.css `(pointer: coarse)`: a touch device's dense buttons grow to
    the 44px target; only a browser with touch emulation matches that media
    query. A mouse keeps the compact layout."""
    context = await browser.new_context(
        base_url=base_url, has_touch=True, viewport={"width": 390, "height": 844}
    )
    page = await context.new_page()
    try:
        assert await page.evaluate("matchMedia('(pointer: coarse)').matches") or (
            await page.goto("/login")
            and await page.evaluate("matchMedia('(pointer: coarse)').matches")
        ), "touch emulation did not make the pointer coarse"
        await sign_in(page, "admin@example.org", "secret-pass-phrase")
        await ready(page)
        await page.goto("/teams/%d" % seeded["team_id"])
        await ready(page)
        # the roster arrives after the handshake and Quasar lays the table
        # out in its own time: poll the boxes rather than measure once
        await page.wait_for_function(
            """() => {
                const b = document.querySelector('button[aria-label^="Remove "]');
                const r = b && b.getBoundingClientRect();
                return !!r && r.width >= 44 && r.height >= 44;
            }"""
        )
        await page.wait_for_function(
            """() => {
                const n = document.querySelector('.vdb-rowbtn');
                return !!n && n.getBoundingClientRect().height >= 44;
            }"""
        )
    finally:
        await context.close()


async def test_a_dialogs_field_row_stacks_on_a_phone(seeded, page):
    """theme.css `.vdb-fields` below 40rem: the new-event dialog's Date,
    Starts and Ends sit side by side on a desktop and one under the other,
    each the width of the card, on a phone."""
    await sign_in(page, "admin@example.org", "secret-pass-phrase")
    await ready(page)
    await page.goto("/events")
    await ready(page)
    # two buttons say New event on an empty parish: the header's and the
    # empty state's; either opens the same dialog
    await page.get_by_role("button", name="New event", exact=True).first.click()
    # the two time fields (the date has a line of its own). Tailwind's
    # runtime writes the width classes a moment after the dialog appears,
    # so wait for the card to reach forms.WIDE before measuring anything
    starts = page.locator(".q-dialog .q-field", has_text="Starts (HH:MM)")
    ends = page.locator(".q-dialog .q-field", has_text="Ends (HH:MM)")
    await expect(starts).to_be_visible()
    await page.wait_for_function(
        "document.querySelector('.q-dialog .q-card')?.getBoundingClientRect().width > 500"
    )
    a, b = await starts.bounding_box(), await ends.bounding_box()
    assert a and b and abs(a["y"] - b["y"]) < 4, "side by side on a desktop"

    await page.set_viewport_size({"width": 390, "height": 844})
    a, b = await starts.bounding_box(), await ends.bounding_box()
    assert a and b and b["y"] > a["y"] + a["height"] - 1, "one under the other"
    card = await page.locator(".q-dialog .q-card").first.bounding_box()
    assert card and a["width"] > card["width"] * 0.8, "the full width of the card"


async def test_the_print_sheet_is_the_page_without_its_chrome(seeded, page):
    """theme.css `@media print`: the header, the buttons and the fields go,
    the ground turns white, and the roster table stays -- what a leader
    pins up. Playwright's print emulation is the only headless way to ask."""
    await sign_in(page, "admin@example.org", "secret-pass-phrase")
    await ready(page)
    await page.goto("/teams/%d" % seeded["team_id"])
    await ready(page)
    await expect(page.locator("thead th", has_text="Name")).to_be_visible()

    await page.emulate_media(media="print")
    await expect(page.locator(".q-header")).to_be_hidden()
    await expect(page.get_by_label("Search the roster…")).to_be_hidden()
    await expect(
        page.get_by_role("button", name="Edit team", exact=True)
    ).to_be_hidden()
    await expect(page.locator("thead th", has_text="Name")).to_be_visible()
    assert await page.evaluate("getComputedStyle(document.body).backgroundColor") in (
        "rgb(255, 255, 255)",
        "rgba(0, 0, 0, 0)",
    )
    await page.emulate_media(media="screen")
    await expect(page.locator(".q-header")).to_be_visible()


# The vertical centre of every field and button in the row under a heading.
# A field's centre is its control's, not its box's: Quasar's box can carry
# 20px of reserved message space under the control (theme.css .vdb-inline).
CONTROL_CENTRES = """(title) => {
    const h = [...document.querySelectorAll('h2')].find(x => x.textContent.trim() === title);
    const row = h.nextElementSibling;
    return [...row.querySelectorAll('.q-field, .q-btn')].map(el => {
        const box = (el.querySelector('.q-field__control') || el).getBoundingClientRect();
        return box.top + box.height / 2;
    });
}"""


async def test_the_add_member_row_centres_its_three_controls(seeded, page):
    """The Volunteer picker is a required field, and Quasar holds a strip
    under a required field for its Required line -- so the picker's box was
    20px taller than Role's and the row centred the two on different lines.
    theme.css drops the strip (.vdb-inline); this checks the cascade did,
    with the Required line hidden and with it showing."""
    await page.set_viewport_size(DESKTOP)
    await sign_in(page, "admin@example.org", "secret-pass-phrase")
    await page.goto(f"/teams/{seeded['team_id']}")
    await ready(page)

    centres = await page.evaluate(CONTROL_CENTRES, "Add member")
    assert len(centres) == 3, "Volunteer, Role, Add"
    assert max(centres) - min(centres) < 1, centres

    # Add with nothing picked: the Required line appears under the picker
    # and overhangs the gap below, moving nothing
    await page.get_by_role("button", name="Add", exact=True).click()
    await expect(page.get_by_text("Required", exact=True)).to_be_visible()
    centres = await page.evaluate(CONTROL_CENTRES, "Add member")
    assert max(centres) - min(centres) < 1, centres


# Each two-column list on the page: per row, the right edge of the first
# thing and the left edge of the second (ui/volunteers_page.py, theme.css
# .vdb-two-col).
TWO_COLUMNS = """() => [...document.querySelectorAll('.vdb-two-col')].map(grid =>
    [...grid.querySelectorAll(':scope > .vdb-two-col-row')].map(row => {
        const [first, second] = [...row.children].map(k => k.getBoundingClientRect());
        return {first_right: first.right, second_left: second ? second.left : null,
                row_right: row.getBoundingClientRect().right};
    }))"""


async def test_the_volunteer_page_lists_read_as_two_left_aligned_columns(seeded, page):
    """ "Serves on" and "If they leave": the team and its role, then the
    Remove button or the leadership badge. The second thing starts at one
    x for every row, just past the longest first thing -- a subgrid lines
    the rows up -- rather than at the far edge of the page, which is where
    a justify-between used to put it. On a phone it goes under the first."""
    async with db_session() as session:
        for name in ("Hospitality", "Parish Picnic Task Force"):
            team = ok(await teams.create(session, SYSTEM, name))
            ok(
                await memberships.assign(
                    session, SYSTEM, seeded["volunteer_id"], team.id, TeamRole.member
                )
            )

    await page.set_viewport_size(DESKTOP)
    await sign_in(page, "admin@example.org", "secret-pass-phrase")
    await page.goto(f"/volunteers/{seeded['volunteer_id']}")
    await ready(page)

    lists = await page.evaluate(TWO_COLUMNS)
    assert len(lists) == 2, "Serves on, and If they leave"
    for rows in lists:
        assert len(rows) == 3
        lefts = {row["second_left"] for row in rows}
        assert len(lefts) == 1, f"one x for the second column, every row: {rows}"
        left = lefts.pop()
        widest = max(row["first_right"] for row in rows)
        assert widest < left <= widest + 48, f"just past the longest entry: {rows}"
        assert left < rows[0]["row_right"] - 300, "and nowhere near the far edge"

    # a fresh load at the phone's width, not a resize: the timeline chart
    # under the lists shrinks to a resize on its own time, and a width
    # measured before it does is the chart's, not the page's
    await page.set_viewport_size({"width": 390, "height": 844})
    await page.goto(f"/volunteers/{seeded['volunteer_id']}")
    await ready(page)
    for rows in await page.evaluate(TWO_COLUMNS):
        for row in rows:
            assert row["second_left"] < row["first_right"], (
                f"on a phone the second thing goes under the first: {row}"
            )
    assert await page.evaluate("document.documentElement.scrollWidth") <= 390
