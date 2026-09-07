"""The suggestion dropdown, whose whole behaviour is focus and keys.

ui/search_box.py carries a docstring about a case it could only describe:
"dismissing with Escape leaves the caret where it was, so clicking the box
again fires no focus event" — which is why the box listens for both. A headless
harness has neither a caret nor a click, and NiceGUI's element filters "ignore
a QMenu's open/closed state" (same module), so open-versus-closed is exactly
what it cannot see. A browser can see nothing else.
"""

import re

from playwright.async_api import expect

from .conftest import ready, sign_in

SEARCH_LABEL = "Find volunteers or teams…"  # the dashboard's box
SUGGESTIONS = ".vdb-suggest"  # the QMenu under it (search_box.py)


async def test_suggestions_open_and_close_and_open_again(seeded, page):
    await sign_in(page, "admin@example.org", "secret-pass-phrase")
    await ready(page)
    search = page.get_by_label(SEARCH_LABEL)
    suggestions = page.locator(SUGGESTIONS)

    # under SUGGEST_MIN_CHARS nothing is offered at all
    await search.fill("M")
    await expect(suggestions).to_be_hidden()

    await search.fill("Alv")
    await expect(suggestions).to_be_visible()
    await expect(suggestions.get_by_text("Maria Alvarez")).to_be_visible()

    # Escape dismisses it without clearing the box
    await search.press("Escape")
    await expect(suggestions).to_be_hidden()
    await expect(search).to_have_value("Alv")

    # ... and coming back to a box that still holds a query brings the last
    # suggestions back, though the caret never left and no focus event fires
    await search.click()
    await expect(suggestions).to_be_visible()

    # picking a volunteer opens the side panel rather than navigating
    await suggestions.get_by_text("Maria Alvarez").click()
    drawer = page.locator(".q-drawer")
    await expect(drawer).to_be_visible()
    await expect(drawer.get_by_text("maria@example.org")).to_be_visible()
    await expect(page).to_have_url(re.compile(r":\d+/$"))  # still the dashboard


async def test_the_teams_search_rides_in_the_address(seeded, page):
    """tables.in_address: typing writes ?q= with history.replaceState, which
    only a browser has; a reload then reads it back into the box."""
    await sign_in(page, "admin@example.org", "secret-pass-phrase")
    await ready(page)
    await page.goto("/teams")
    await ready(page)
    box = page.get_by_label("Search teams…")
    await box.fill("zzz")
    await expect(page).to_have_url(re.compile(r"/teams\?q=zzz$"))
    await expect(page.get_by_text("0 of 1 teams")).to_be_visible()

    await page.reload()
    await ready(page)
    await expect(page.get_by_label("Search teams…")).to_have_value("zzz")
    await expect(page.get_by_text("0 of 1 teams")).to_be_visible()

    # clearing the box clears the address too
    await page.get_by_label("Search teams…").fill("")
    await expect(page).to_have_url(re.compile(r"/teams$"))


async def test_the_search_box_is_a_combobox_the_keyboard_can_drive(seeded, page):
    """ui/search_box.py: the native input carries the combobox role and
    state, and ArrowDown / Enter reach an option -- which only a browser,
    with its focus and its keys, can show end to end."""
    await sign_in(page, "admin@example.org", "secret-pass-phrase")
    await ready(page)
    search = page.get_by_role("combobox", name=SEARCH_LABEL)
    await expect(search).to_have_attribute("aria-expanded", "false")
    await search.fill("Alv")
    await expect(page.locator(SUGGESTIONS)).to_be_visible()
    await expect(search).to_have_attribute("aria-expanded", "true")
    listbox = page.locator(SUGGESTIONS).get_by_role("listbox")
    await expect(listbox).to_have_count(1)
    assert await search.get_attribute("aria-controls") == await listbox.get_attribute(
        "id"
    )

    await search.press("ArrowDown")
    marked = page.locator(f"{SUGGESTIONS} .vdb-active")
    await expect(marked).to_have_count(1)
    assert await search.get_attribute("aria-activedescendant") == (
        await marked.get_attribute("id")
    )
    await expect(marked).to_contain_text("Maria Alvarez")
    await search.press("Enter")
    drawer = page.locator(".q-drawer")
    await expect(drawer).to_be_visible()
    await expect(drawer.get_by_text("maria@example.org")).to_be_visible()
