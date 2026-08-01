const TINT_OPACITY = 0.45;

// Semi-transparent solid-colour layer drawn OVER a node's photo so the
// capacity band stays readable. Kept at module level: styleFor() is re-run
// by the dark-mode observer and must not rebuild closures per restyle.
function tintLayer(color) {
  const svg =
    '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16">' +
    `<rect width="16" height="16" fill="${color}" fill-opacity="${TINT_OPACITY}"/></svg>`;
  return "data:image/svg+xml;utf8," + encodeURIComponent(svg);
}

export default {
  template: '<div style="width: 100%; height: 70vh; min-height: 420px;"></div>',
  props: {
    elements: Object,
  },
  async mounted() {
    const cytoscape = (await import("/static/cytoscape.esm.min.js")).default;
    this.cy = cytoscape({
      container: this.$el,
      elements: this.elements,
      style: this.styleFor(this.themeColors()),
      layout: this.layoutOptions(),
      wheelSensitivity: 0.2,
    });
    this.cy.on("tap", "node", (e) => this.$emit("node_click", e.target.data()));
    // Quasar's dark toggle flips body.body--dark; restyle in place
    this.themeObserver = new MutationObserver(() => {
      if (this.cy) this.cy.style(this.styleFor(this.themeColors()));
    });
    this.themeObserver.observe(document.body, { attributes: true, attributeFilter: ["class"] });
  },
  beforeUnmount() {
    if (this.themeObserver) this.themeObserver.disconnect();
    if (this.cy) this.cy.destroy();
  },
  methods: {
    themeColors() {
      const styles = getComputedStyle(document.body);
      const read = (name, fallback) => styles.getPropertyValue(name).trim() || fallback;
      return {
        team: read("--vdb-graph-team", "#a5573e"),
        teamLabel: read("--vdb-graph-team-label", "#fdf6e3"),
        node: read("--vdb-graph-node", "#9e9e9e"),
        label: read("--vdb-graph-label", "#333333"),
        edge: read("--vdb-graph-edge", "#d8c9a3"),
        leader: read("--vdb-graph-leader", "#b07d2b"),
        hier: read("--vdb-graph-hier", "#8a7550"),
        selected: read("--vdb-graph-selected", "#2e5e7e"),
      };
    },
    styleFor(c) {
      const serif = 'Palatino, "Palatino Linotype", "Book Antiqua", Georgia, serif';
      return [
        {
          selector: 'node[type="team"]',
          style: {
            shape: "round-rectangle",
            "background-color": c.team,
            label: "data(label)",
            color: c.teamLabel,
            "text-valign": "center",
            "text-halign": "center",
            "font-size": "12px",
            "font-family": serif,
            "font-weight": "bold",
            width: "label",
            height: "28px",
            padding: "8px",
          },
        },
        {
          selector: 'node[type="volunteer"]',
          style: {
            shape: "ellipse",
            "background-color": c.node,
            label: "data(label)",
            color: c.label,
            "font-size": "10px",
            "font-family": serif,
            "text-valign": "bottom",
            "text-margin-y": "4px",
            width: "16px",
            height: "16px",
          },
        },
        {
          // capacity colouring; nodes without a color datum keep the neutral above
          selector: 'node[type="volunteer"][color]',
          style: { "background-color": "data(color)" },
        },
        {
          // headshot fills the bubble; capacity tint (when visible) is a second
          // image layer drawn on top of it. Model size stays 16px — the canvas
          // renders the photo at screen resolution, so it resolves on zoom.
          selector: 'node[type="volunteer"][photo]',
          style: {
            "background-image": (ele) =>
              ele.data("color")
                ? [ele.data("photo"), tintLayer(ele.data("color"))]
                : [ele.data("photo")],
            "background-fit": (ele) => (ele.data("color") ? "cover cover" : "cover"),
            "background-clip": "node",
          },
        },
        {
          selector: "edge",
          style: {
            width: 1,
            "line-color": c.edge,
            "curve-style": "haystack",
          },
        },
        {
          selector: "edge[?leadership]",
          style: { width: 2.5, "line-color": c.leader },
        },
        {
          selector: "edge[?hierarchy]",
          style: {
            width: 2,
            "line-color": c.hier,
            "line-style": "dashed",
            "curve-style": "bezier",
            "target-arrow-shape": "triangle",
            "target-arrow-color": c.hier,
          },
        },
        {
          selector: "node:selected",
          style: { "border-width": 3, "border-color": c.selected },
        },
      ];
    },
    layoutOptions() {
      return { name: "cose", animate: false, nodeOverlap: 8, idealEdgeLength: 60 };
    },
    refresh(elements) {
      if (!this.cy) return;
      this.cy.elements().remove();
      this.cy.add(elements);
      this.cy.layout(this.layoutOptions()).run();
      this.cy.fit(undefined, 30);
    },
  },
};
