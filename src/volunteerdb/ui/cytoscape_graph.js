export default {
  template: '<div style="width: 100%; height: 70vh; min-height: 420px;"></div>',
  props: {
    elements: Object,
    libUrl: String,
  },
  async mounted() {
    // libUrl carries a cache-busting ?v= and matches the page's modulepreload
    const cytoscape = (
      await import(this.libUrl || "/static/cytoscape.esm.min.js")
    ).default;
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
    // the canvas caches its container size at init; follow later layout
    // changes, and fit once when the element first gains real dimensions
    // (e.g. mounted inside a container that was hidden or still reflowing)
    this.hadSize = this.$el.clientWidth > 0 && this.$el.clientHeight > 0;
    this.resizeObserver = new ResizeObserver(() => {
      if (!this.cy) return;
      this.cy.resize();
      const hasSize = this.$el.clientWidth > 0 && this.$el.clientHeight > 0;
      if (hasSize && !this.hadSize) this.cy.fit(undefined, 30);
      this.hadSize = hasSize;
    });
    this.resizeObserver.observe(this.$el);
  },
  beforeUnmount() {
    if (this.themeObserver) this.themeObserver.disconnect();
    if (this.resizeObserver) this.resizeObserver.disconnect();
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
          // workload colouring; nodes without a color datum keep the neutral above
          selector: 'node[type="volunteer"][color]',
          style: { "background-color": "data(color)" },
        },
        {
          // headshot fills the bubble. Model size stays 16px — the canvas
          // renders the photo at screen resolution, so it resolves on zoom.
          selector: 'node[type="volunteer"][photo]',
          style: {
            "background-image": "data(photo)",
            "background-fit": "cover",
            "background-clip": "node",
          },
        },
        {
          // workload ring around the headshot — a tint over the photo is
          // illegible, so the band colour becomes an outline instead.
          // node:selected sits later in this sheet and overrides it.
          selector: 'node[type="volunteer"][photo][color]',
          style: {
            "border-width": 2.5,
            "border-color": "data(color)",
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
    layoutOptions(numIter = 300) {
      // cose defaults to numIter 1000; the layout runs synchronously on the
      // main thread, so iterations are paid for in time-to-first-render
      return {
        name: "cose",
        animate: false,
        nodeOverlap: 8,
        idealEdgeLength: 60,
        numIter,
      };
    },
    refresh(elements) {
      if (!this.cy) return;
      // carry surviving nodes' positions across the swap: cose keeps
      // randomize:false, so it refines from where nodes already are instead
      // of re-annealing the whole map (and re-scrambling it) on every filter
      const pos = {};
      this.cy.nodes().forEach((n) => {
        pos[n.id()] = { ...n.position() };
      });
      this.cy.elements().remove();
      this.cy.add(elements);
      let fresh = 0;
      this.cy.nodes().forEach((n) => {
        if (pos[n.id()]) n.position(pos[n.id()]);
        else fresh += 1;
      });
      this.cy.layout(this.layoutOptions(fresh ? 300 : 100)).run();
      this.cy.fit(undefined, 30);
    },
    fit() {
      if (this.cy) this.cy.fit(undefined, 30);
    },
  },
};
