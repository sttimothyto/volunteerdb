"""The manual's search endpoint.

One GET, not a NiceGUI page: the search box in the built manual's sidebar
(docs/_static/vdb-manual.js) fetches it with the browser's own session
cookie and draws the hits itself, so the reply is JSON and nothing here
renders.

    GET /manual/_search?q=…&audience=user|all

        {"query": "…", "results": [{"title", "section", "url",
                                    "snippet", "audience"}, …]}

Under /manual on purpose: the AuthMiddleware sends an anonymous caller to
/login exactly as it does for the pages, and the script reads that HTML
reply as "unavailable" and falls back to Sphinx's own search page. Wired
from main.create_app() BEFORE the /manual mount, not from register_pages():
Starlette dispatches to the first route that matches, in registration
order, and a Mount matches everything under its prefix.
"""

from nicegui import app
from starlette.responses import JSONResponse

from ..manual_search import search, snippet, tokenize

# A query is what somebody typed into a box, not a document.
MAX_QUERY = 200
AUDIENCES = frozenset({"user", "all"})
# The hits depend on the audience asked for and on the manual baked into the
# running image; neither is anything a cache should hold on to.
NO_STORE = {"Cache-Control": "no-store"}


async def search_manual(q: str = "", audience: str = "user") -> JSONResponse:
    # Both parameters default: a route under the page tree may not require a
    # query parameter (tests/test_app_surface.py), so an empty q is a 400
    # of our own rather than FastAPI's 422.
    query = " ".join(q.split())
    if not query:
        return _bad_request("q is required")
    if len(query) > MAX_QUERY:
        return _bad_request(f"q is longer than {MAX_QUERY} characters")
    if audience not in AUDIENCES:
        return _bad_request("audience must be user or all")
    index = await app.state.manual_index.get()
    terms = tokenize(query)
    results = [
        {
            "title": chunk.title,
            "section": chunk.section,
            "url": chunk.url,
            "snippet": snippet(chunk.text, terms),
            "audience": chunk.audience,
        }
        for chunk in search(index, query, audience=audience)
    ]
    return JSONResponse({"query": query, "results": results}, headers=NO_STORE)


def _bad_request(detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=400, headers=NO_STORE)


def register() -> None:
    """Wire the route onto the global app; called from main.create_app() on
    every create_app() (see ministries_routes.register for why)."""
    app.get("/manual/_search", include_in_schema=False)(search_manual)
