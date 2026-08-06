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

_PUBLIC = {("POST", "/api/auth/login")}  # the only unauthenticated route


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

    response = await real_app_client.get(
        "/static/cytoscape.esm.min.js", follow_redirects=False
    )
    assert response.status_code == 200, (
        "the /static mount must serve without auth, or every page loads unstyled"
    )
