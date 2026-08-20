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
