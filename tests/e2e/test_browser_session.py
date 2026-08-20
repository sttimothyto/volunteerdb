"""Signing in, staying signed in, and how this browser likes to read.

The headless suite already proves who may sign in (tests/test_ui_auth.py). What
only a browser can show is that the session actually survives the round trip
through a cookie, and that the preferences riding on the same storage — dark
mode, kept across a sign-out on purpose — behave the way ui/context.py says.
"""

import re

from playwright.async_api import expect

from .conftest import icon_button, ready, sign_in

# Chromium hands back the decoded path, so the %2F the middleware quoted is a /
# again by the time it reaches page.url.
LOGIN_WITH_REDIRECT = re.compile(r"/login\?redirect_to=(%2F|/)volunteers$")


async def test_signing_in_and_out_through_the_real_form(seeded, page):
    # anonymous: AuthMiddleware bounces the browser to the login card, and
    # remembers where it was going
    await page.goto("/volunteers")
    await expect(page).to_have_url(LOGIN_WITH_REDIRECT)
    await ready(page)

    # wrong password: told so, and still on the card
    await page.get_by_label("Email").fill("admin@example.org")
    await page.get_by_label("Password (optional)").fill("wrong-pass-phrase")
    await page.get_by_role("button", name="Sign in", exact=True).click()
    await expect(page.get_by_text("Invalid email or password")).to_be_visible()

    # right password: redirect_to puts us where we were going, not on the
    # dashboard — the round trip through the socket and back out to a redirect
    await page.get_by_label("Password (optional)").fill("secret-pass-phrase")
    await page.get_by_role("button", name="Sign in", exact=True).click()
    await expect(page).to_have_url(re.compile(r"/volunteers$"))
    await expect(page.get_by_text("1 volunteer")).to_be_visible()

    # a reload is the whole point of the cookie: no socket, no page state, and
    # still signed in
    await page.reload()
    await expect(page.get_by_text("1 volunteer")).to_be_visible()
    await ready(page)

    # and out again, from the header
    await icon_button(page, "logout").click()
    await expect(page).to_have_url(re.compile(r"/login$"))
    await page.goto("/volunteers")
    await expect(page).to_have_url(LOGIN_WITH_REDIRECT)


async def test_dark_mode_is_kept_by_the_browser_across_a_sign_out(seeded, page):
    """clear_session() keeps the dark-mode preference deliberately: "that is how
    this browser likes to read, whoever is signed in". Nothing headless can see
    it, because what applies it is Quasar toggling a class on <body>."""
    await sign_in(page, "admin@example.org", "secret-pass-phrase")
    await expect(page.get_by_text("Dashboard")).to_be_visible()
    await ready(page)
    body = page.locator("body")
    await expect(body).not_to_have_class(re.compile(r"body--dark"))

    await icon_button(page, "settings").click()
    await page.get_by_text("Dark mode").click()
    await expect(body).to_have_class(re.compile(r"body--dark"))

    # persisted server-side against the session id in the cookie, so it comes
    # back on a page the switch never touched
    await page.goto("/volunteers")
    await expect(body).to_have_class(re.compile(r"body--dark"))

    # ... and survives signing out, unlike everything else in that storage
    await ready(page)
    await icon_button(page, "logout").click()
    await expect(page).to_have_url(re.compile(r"/login$"))
    await expect(body).to_have_class(re.compile(r"body--dark"))
