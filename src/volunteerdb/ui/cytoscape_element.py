from collections.abc import Callable

from nicegui.element import Element
from nicegui.events import GenericEventArguments

from .assets import static_url


class CytoscapeGraph(Element, component="cytoscape_graph.js"):
    """Cytoscape.js network view. The library itself is served from
    /static/cytoscape.esm.min.js (see app.add_static_files in main.py)."""

    def __init__(
        self,
        elements: dict,
        on_node_click: Callable[[GenericEventArguments], None] | None = None,
    ) -> None:
        super().__init__()
        self._props["elements"] = elements
        # must match the dashboard's modulepreload href byte-for-byte —
        # the query string is part of the browser's cache key
        self._props["libUrl"] = static_url("cytoscape.esm.min.js")
        if on_node_click is not None:
            self.on("node_click", on_node_click)

    def refresh(self, elements: dict) -> None:
        self._props["elements"] = elements
        self.run_method("refresh", elements)

    def fit(self) -> None:
        """Reset zoom/pan so the whole graph is in view."""
        self.run_method("fit")
