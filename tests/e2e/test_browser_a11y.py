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
from playwright.async_api import Page

from .conftest import icon_button, ready, sign_in

PAGES = ("/", "/events", "/teams", "/volunteers", "/account")
FAIL_ON = ("serious", "critical")


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
    minor = _findings(results, ("minor", "moderate"))
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
async def test_the_signed_in_pages(seeded, page: Page, base_url: str, dark: bool):
    await page.set_viewport_size({"width": 1280, "height": 900})
    await sign_in(page, "admin@example.org", "secret-pass-phrase")
    await ready(page)
    if dark:
        await icon_button(page, "settings").click()
        await page.get_by_role("switch", name="Dark mode").click()
        await page.keyboard.press("Escape")
        await page.wait_for_selector("body.body--dark")
    for path in PAGES:
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
    for name in ("Settings", "Sign out"):
        assert await page.get_by_role("button", name=name).count() == 1, name
