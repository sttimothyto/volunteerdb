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

from .conftest import icon_button, ready, sign_in

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

    # one row: the sign-out button sits level with the brand, not under it
    brand = await page.locator("header").get_by_text("Dash").bounding_box()
    sign_out = await icon_button(page, "logout").bounding_box()
    assert brand and sign_out
    assert abs(brand["y"] - sign_out["y"]) < brand["height"], (
        f"the header wrapped: brand at y={brand['y']:.0f}, sign out at "
        f"y={sign_out['y']:.0f}"
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
