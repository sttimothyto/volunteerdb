"""The ministry graph: 400 KB of Cytoscape that only a browser ever executes.

Every other test of the dashboard stops at the elements dict the service hands
over (tests/test_reports_and_graph.py). Past that point the whole feature is
ui/cytoscape_graph.js — a dynamic import, a canvas, and a `node_click` event
that has to find its way back over the socket — and nothing headless runs a
line of it. So this asks the drawn graph what it is showing, and clicks it.
"""

import pytest
from playwright.async_api import expect

from volunteerdb.db import db_session
from volunteerdb.models import TeamRole
from volunteerdb.services import memberships, volunteers

from .conftest import ready, sign_in

# Cytoscape draws each layer on its own canvas and tags it; the node layer is
# the one that exists only once the library has booted and laid the graph out.
NODE_LAYER = 'canvas[data-id="layer2-node"]'

# The live cytoscape instance, reached the way column_drag.js reaches a table:
# every NiceGUI element renders as id="c<id>", nothing between the canvas and
# the component's root div is one, and getElement() hands back the Vue
# component — where mounted() left `this.cy`.
FIND_GRAPH = (
    """
() => {
    let el = document.querySelector('%s');
    while (el && !/^c\\d+$/.test(el.id)) el = el.parentElement;
    return el && getElement(Number(el.id.slice(1)))?.cy ? el.id : null;
}
"""
    % NODE_LAYER
)

COUNT_ELEMENTS = """
(id) => {
    const cy = getElement(Number(id.slice(1))).cy;
    return {nodes: cy.nodes().length, edges: cy.edges().length};
}
"""

# Where a named volunteer's dot ended up, in pixels from the container's corner
# — the layout is force-directed, so nothing but the graph itself knows.
LOCATE_NODE = """
([id, label]) => {
    const cy = getElement(Number(id.slice(1))).cy;
    const node = cy.nodes().filter(n => n.data('label') === label)[0];
    if (!node) return null;
    const at = node.renderedPosition();
    return {x: at.x, y: at.y};
}
"""


# The graph fits itself to its container the first time that container has real
# dimensions (the ResizeObserver in cytoscape_graph.js), and a fit moves every
# dot on the screen. On a loaded machine that can land between reading a
# position and clicking it, so wait for the dot to hold still first: two reads
# a poll apart that agree.
NODE_IS_STILL = """
([id, label]) => {
    const cy = getElement(Number(id.slice(1))).cy;
    const node = cy.nodes().filter(n => n.data('label') === label)[0];
    if (!node) return false;
    const at = node.renderedPosition();
    const now = Math.round(at.x) + ',' + Math.round(at.y);
    const before = window.__vdbNodeWas;
    window.__vdbNodeWas = now;
    return before === now;
}
"""


# Cytoscape keeps its own idea of where its container sits in the window, and
# refreshes it from a scroll listener of its own — measured at ~30 ms behind
# the scroll. Inside that window the library measures the pointer against the
# old rectangle, decides the click happened outside the graph, and drops it
# before it can become a tap: no node event, not even a background one. A
# reader who scrolls down to the graph and then takes aim is long past it; a
# test that scrolls and clicks in the same frame sees nothing happen at all,
# with no error to explain why.
#
# Reaching into cy.renderer() is deliberate: the library is vendored
# (ui/static/cytoscape.esm.min.js), so this cannot drift under us silently, and
# the alternative is a sleep long enough to be a guess.
GRAPH_KNOWS_WHERE_IT_IS = """
(id) => {
    const cy = getElement(Number(id.slice(1))).cy;
    const cached = cy.renderer().findContainerClientCoords();
    const rect = document.getElementById(id).getBoundingClientRect();
    // 2px of slack for the container's 1px border, which sits outside the
    // rectangle cytoscape measures
    return Math.abs(cached[0] - rect.left) <= 2
        && Math.abs(cached[1] - rect.top) <= 2;
}
"""


async def settled_position(page, container: str, label: str) -> dict:
    """The dot's position, once it has stopped moving."""
    await page.wait_for_function(NODE_IS_STILL, arg=[container, label], polling=200)
    at = await page.evaluate(LOCATE_NODE, [container, label])
    assert at, "the graph drew no node with that label"
    return at


async def aim_at(page, container: str, label: str) -> dict:
    """Scroll the graph into view and wait until it can be clicked, then say
    where the named dot is."""
    at = await settled_position(page, container, label)
    graph = page.locator(f"#{container}")
    await graph.scroll_into_view_if_needed()
    await page.wait_for_function(GRAPH_KNOWS_WHERE_IT_IS, arg=container, polling=50)
    return at


@pytest.fixture
async def parish(seeded):
    """The seeded parish plus a second pair of hands, so the graph has more
    than one dot to tell apart and an edge that is not the only edge."""
    async with db_session() as session:
        felix = await volunteers.create(
            session, None, "Felix", "Garcia", "felix@example.org"
        )
        await memberships.assign(
            session, None, felix.id, seeded["team_id"], TeamRole.leader
        )
    return seeded


async def test_the_graph_draws_the_parish_and_opens_a_volunteer_on_a_click(
    parish, page
):
    await sign_in(page, "admin@example.org", "secret-pass-phrase")
    await ready(page)

    # the library loads by dynamic import() at mount, so the canvas appearing
    # is the proof that /static/cytoscape.esm.min.js arrived and ran
    await expect(page.locator(NODE_LAYER)).to_be_visible()
    container = await page.evaluate(FIND_GRAPH)
    assert container, "cytoscape never initialised on the dashboard"

    # one team, two volunteers, and an edge per membership
    assert await page.evaluate(COUNT_ELEMENTS, container) == {"nodes": 3, "edges": 2}

    # clicking a dot is a cytoscape tap -> $emit -> socket -> panel.open, which
    # is the whole round trip this file exists for
    at = await aim_at(page, container, "Felix Garcia")
    await page.locator(f"#{container}").click(position=at)

    drawer = page.locator(".q-drawer")
    await expect(drawer).to_be_visible()
    await expect(drawer.get_by_text("Felix Garcia")).to_be_visible()
    await expect(drawer.get_by_text("Liturgy")).to_be_visible()


async def test_the_graph_library_is_fetched_once(parish, page):
    """ui/cytoscape_element.py: the libUrl "must match the dashboard's
    modulepreload href byte-for-byte — the query string is part of the
    browser's cache key". Drift the two apart and everything still works,
    except that every visitor downloads 400 KB twice; only a browser's own
    network log can tell you so."""
    requested: list[str] = []
    page.on(
        "request",
        lambda request: (
            requested.append(request.url)
            if "cytoscape.esm.min.js" in request.url
            else None
        ),
    )

    await sign_in(page, "admin@example.org", "secret-pass-phrase")
    await ready(page)
    await expect(page.locator(NODE_LAYER)).to_be_visible()

    preload = await page.get_attribute('link[rel="modulepreload"]', "href")
    assert set(requested) == {f"{page.url.rstrip('/')}{preload}"}, (
        "the preload and the import() disagree, so the browser fetched the "
        f"graph library more than once: {sorted(set(requested))}"
    )
