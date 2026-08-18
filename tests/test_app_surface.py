"""The app as assembled, not just its routers.

conftest's `client` drives a bare FastAPI carrying only the API routers, which
proves the routers behave but not that create_app() wires them up. Drop "/api/"
from main.UNRESTRICTED_PREFIXES and every other API test still passes while
production bounces every API call to the login page. These two tests cover that
seam: no route answers an anonymous caller, and the real app still routes.
"""

import re

# Path-item keys that are operations; the rest ("parameters", "summary", ...)
# describe the path itself.
_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}

# Routes that answer without a Bearer token, because the *body* carries the
# credential and the caller has no session yet: signing in, spending an invite
# link, and confirming an address change from whatever mailbox read the mail.
# Everything else needs CtxDep.
_PUBLIC = {
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/redeem-invite"),
    ("POST", "/api/auth/email-change/confirm"),
}


def _concrete(path: str) -> str:
    return re.sub(r"\{(\w+)\}", "1", path)  # every placeholder is an integer id


async def test_every_api_route_rejects_an_anonymous_request(client, api_app):
    """Self-maintaining authz sweep: a new endpoint that forgets CtxDep fails
    here the moment it is added, without anyone remembering to test it."""
    paths = api_app.openapi()["paths"]
    checked = 0
    for path, operations in sorted(paths.items()):
        for method in operations:
            if method.lower() not in _METHODS or (method.upper(), path) in _PUBLIC:
                continue
            response = await client.request(method.upper(), _concrete(path))
            checked += 1
            assert response.status_code == 401, (
                f"{method.upper()} {path} answered an anonymous caller with "
                f"{response.status_code} — every route except the login needs CtxDep"
            )
            assert response.headers.get("WWW-Authenticate") == "Bearer", (
                f"{method.upper()} {path} rejected the caller without a Bearer challenge"
            )

    # 41 operations today. The floor guards against the sweep silently
    # degenerating to nothing if the openapi() seam changes, not against
    # legitimately removing a route.
    assert checked >= 30, (
        f"only swept {checked} operations — openapi() stopped reporting the routes and "
        "this test is no longer covering the API surface"
    )


async def test_real_app_serves_the_api_and_guards_the_pages(real_app_client):
    """create_app() end to end: middleware, mounts and page routes together."""
    response = await real_app_client.get("/api/teams", follow_redirects=False)
    assert response.status_code == 401, (
        "AuthMiddleware must leave /api alone (main.UNRESTRICTED_PREFIXES) — a redirect "
        "here means every API client gets bounced to the login page instead of a 401"
    )

    for guarded in ("/volunteers", "/manual", "/photos/1"):
        response = await real_app_client.get(guarded, follow_redirects=False)
        assert response.status_code in (302, 303, 307), (
            f"{guarded} served an anonymous browser with {response.status_code}"
        )
        assert "/login" in response.headers.get("location", ""), (
            f"{guarded} must send anonymous browsers to the login page"
        )

    for asset in ("cytoscape.esm.min.js", "column_drag.js"):
        response = await real_app_client.get(f"/static/{asset}", follow_redirects=False)
        assert response.status_code == 200, (
            "the /static mount must serve without auth, or every page loads unstyled"
        )

    # the ministries pages are deliberately public (UNRESTRICTED_PREFIXES):
    # anonymous parishioners read them without an account
    response = await real_app_client.get("/ministries/", follow_redirects=False)
    assert response.status_code == 200, (
        "/ministries/ must serve anonymous browsers, not bounce them to /login"
    )
    response = await real_app_client.get(
        "/ministries/never-heard-of-it.html", follow_redirects=False
    )
    assert response.status_code == 404, (
        "an unknown ministry page is a 404, not a login redirect"
    )
    response = await real_app_client.get("/ministries/img/1/1", follow_redirects=False)
    assert response.status_code == 404, (
        "an unknown ministry image is a 404, not a login redirect"
    )

    # the address-confirmation link is opened from whatever browser reads the
    # new mailbox, days later, signed out — bouncing it to /login would ask
    # the reader to sign in at the address they are trying to replace
    response = await real_app_client.get(
        "/confirm-email/not-a-real-token", follow_redirects=False
    )
    assert response.status_code == 200, (
        "/confirm-email/ must render for anonymous browsers "
        "(main.UNRESTRICTED_PREFIXES); a dead token is a message, not a redirect"
    )


async def test_the_login_page_hands_out_a_fresh_session_id(real_app_client):
    """Session fixation. NiceGUI assigns a session id on the first request and
    never changes it, and app.storage.user is keyed by it — so an id planted on
    a browser (shared kiosk, sibling-subdomain XSS, an active network attacker
    where VDB_COOKIE_SECURE is off) would otherwise become the *authenticated*
    id the moment the victim signed in. Loading an auth entry point while
    anonymous discards it, which is the one place a Set-Cookie can still be
    sent: sign-in itself happens over the websocket.

    Pins the reach into nicegui.storage in main.AuthMiddleware. If NiceGUI
    changes how the session id is stored, this fails rather than quietly
    dropping the protection."""
    cookie_name = "session"
    first = await real_app_client.get("/login", follow_redirects=False)
    assert first.status_code == 200
    planted = real_app_client.cookies.get(cookie_name)
    assert planted, "the login page issues a session cookie"

    second = await real_app_client.get("/login", follow_redirects=False)
    assert second.status_code == 200
    assert real_app_client.cookies.get(cookie_name) != planted, (
        "an anonymous visit to /login must mint a new session id, not reuse the "
        "one the browser turned up holding"
    )

    for entry in ("/invite/nope", "/confirm-email/nope"):
        before = real_app_client.cookies.get(cookie_name)
        await real_app_client.get(entry, follow_redirects=False)
        assert real_app_client.cookies.get(cookie_name) != before, (
            f"{entry} authenticates a browser too, so it rotates as well"
        )


async def test_a_signed_in_browser_is_not_rotated_off_its_own_session(
    real_app_client, database
):
    """The other half of the rotation rule, and the one that would hurt: only
    ANONYMOUS browsers are given a new id. A signed-in reader who follows a
    stale /login link, or lands on a dead invite link, must come away still
    signed in — rotating there would carry nothing over and read as a random
    logout."""
    from volunteerdb.db import db_session
    from volunteerdb.services import users

    async with db_session() as session:
        user, _ = await users.create(session, "stays@example.org")
        user_id = user.id

    await real_app_client.get(f"/login-dev/{user_id}", follow_redirects=False)
    signed_in = real_app_client.cookies.get("session")
    assert signed_in

    for entry in ("/login", "/invite/nope", "/confirm-email/nope"):
        await real_app_client.get(entry, follow_redirects=False)
        assert real_app_client.cookies.get("session") == signed_in, (
            f"{entry} rotated a signed-in browser's session id"
        )

    # and the session still works
    response = await real_app_client.get("/volunteers", follow_redirects=False)
    assert response.status_code == 200, "still signed in after visiting /login"


async def test_every_page_opens_with_nothing_but_its_path(real_app_client):
    """A browser types a URL; it cannot supply anything else.

    So a page route may not carry a *required* parameter that is not in its own
    path — FastAPI would read it as a mandatory query string and answer 422 to
    every visitor. That is not a hypothetical: extracting a section out of a
    long page function once left `@ui.page("/elections/{proposal_id}")` sitting
    on the extracted helper, and the whole page answered 422 until this rule was
    written down. The same check catches a path placeholder the handler forgot to
    accept, and (via the name rule) a decorator that slid onto its neighbour.
    """
    from fastapi.routing import APIRoute
    from nicegui import app

    # /api is Bearer-authenticated JSON with its own contract test; the
    # _nicegui/ and /static/ routes belong to the framework, not to us.
    ours = [
        route
        for route in app.routes
        if isinstance(route, APIRoute)
        and not route.path.startswith(("/api", "/_nicegui", "/static"))
    ]
    assert len(ours) > 15, "the page routes did not register — is the app up?"

    for route in ours:
        required = [
            field.name
            for field in route.dependant.query_params
            if field.field_info.is_required()
        ]
        assert not required, (
            f"{route.path} ({route.endpoint.__name__}) requires the query "
            f"parameter(s) {required}: every visitor gets a 422. Give them a "
            "default, or take the value from the path."
        )
        accepted = {field.name for field in route.dependant.path_params}
        declared = set(re.findall(r"\{(\w+)", route.path))
        assert declared <= accepted, (
            f"{route.path} names {declared - accepted} in its path but "
            f"{route.endpoint.__name__} does not accept it"
        )
        # page handlers are the module's public names; the section helpers they
        # call are underscore-prefixed, so a decorator that came adrift lands on
        # one of those and shows up here
        assert not route.endpoint.__name__.startswith(
            "_"
        ) or route.endpoint.__name__ in {"_manual_not_built"}, (
            f"{route.path} is served by {route.endpoint.__name__} — a helper, not a page"
        )
