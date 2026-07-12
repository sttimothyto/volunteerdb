from collections.abc import Callable

from nicegui.element import Element
from nicegui.events import GenericEventArguments


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
        if on_node_click is not None:
            self.on("node_click", on_node_click)

    def refresh(self, elements: dict) -> None:
        self._props["elements"] = elements
        self.run_method("refresh", elements)
