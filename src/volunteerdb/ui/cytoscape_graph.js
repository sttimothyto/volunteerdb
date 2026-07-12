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
      style: [
        {
          selector: 'node[type="team"]',
          style: {
            shape: "round-rectangle",
            "background-color": "#1976d2",
            label: "data(label)",
            color: "#fff",
            "text-valign": "center",
            "text-halign": "center",
            "font-size": "11px",
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
            "background-color": "#9e9e9e",
            label: "data(label)",
            "font-size": "9px",
            "text-valign": "bottom",
            "text-margin-y": "4px",
            width: "16px",
            height: "16px",
          },
        },
        {
          selector: "edge",
          style: {
            width: 1,
            "line-color": "#cfd8dc",
            "curve-style": "haystack",
          },
        },
        {
          selector: "edge[?leadership]",
          style: { width: 2.5, "line-color": "#ef6c00" },
        },
        {
          selector: "edge[?hierarchy]",
          style: {
            width: 2,
            "line-color": "#1976d2",
            "line-style": "dashed",
            "curve-style": "bezier",
            "target-arrow-shape": "triangle",
            "target-arrow-color": "#1976d2",
          },
        },
        {
          selector: "node:selected",
          style: { "border-width": 3, "border-color": "#d32f2f" },
        },
      ],
      layout: this.layoutOptions(),
      wheelSensitivity: 0.2,
    });
    this.cy.on("tap", "node", (e) => this.$emit("node_click", e.target.data()));
  },
  beforeUnmount() {
    if (this.cy) this.cy.destroy();
  },
  methods: {
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
