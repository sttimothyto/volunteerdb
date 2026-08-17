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
      elements: this.annotated(this.elements),
      style: this.styleFor(this.themeColors()),
      layout: this.layoutOptions(),
      wheelSensitivity: 0.2,
    });
    this.cy.on("tap", "node", (e) => this.$emit("node_click", e.target.data()));
    // hovering a node isolates its neighbourhood: at parish scale the whole
    // map is a mesh, and "who is actually on this team" is unanswerable
    // without dimming everything that is not the answer
    this.cy.on("mouseover", "node", (e) => this.focusOn(e.target));
    this.cy.on("mouseout", "node", () => this.clearFocus());
    // a pointer that leaves the canvas mid-hover never fires the node's own
    // mouseout, which would strand the graph in a dimmed state
    this.onLeave = () => this.clearFocus();
    this.$el.addEventListener("mouseleave", this.onLeave);
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
    if (this.onLeave) this.$el.removeEventListener("mouseleave", this.onLeave);
    if (this.themeObserver) this.themeObserver.disconnect();
    if (this.resizeObserver) this.resizeObserver.disconnect();
    if (this.cy) this.cy.destroy();
  },
  methods: {
    // Degree, computed here rather than server-side: it is a property of the
    // elements already on the wire, and the styles below map it to size so a
    // hub reads as a hub. Counted before cytoscape sees the elements, so the
    // layout runs against the sizes it will actually draw. Both counts are
    // clamped to the mapData domain — cytoscape extrapolates past a range's
    // end rather than clamping, and one person on nine teams should not
    // become a planet.
    annotated(elements) {
      const links = {};
      const roster = {};
      for (const edge of elements.edges || []) {
        if (edge.data.hierarchy) continue; // team -> parent team, not a person
        links[edge.data.source] = (links[edge.data.source] || 0) + 1;
        roster[edge.data.target] = (roster[edge.data.target] || 0) + 1;
      }
      const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));
      const nodes = (elements.nodes || []).map((node) => {
        const id = node.data.id;
        const extra =
          node.data.type === "team"
            ? { roster: clamp(roster[id] || 0, 0, 30) }
            : { links: clamp(links[id] || 1, 1, 5) };
        return { ...node, data: { ...node.data, ...extra } };
      });
      return { nodes, edges: elements.edges || [] };
    },
    focusOn(node) {
      const keep = node.closedNeighborhood();
      this.cy.batch(() => {
        this.cy.elements().difference(keep).addClass("vdb-dim");
        keep.addClass("vdb-focus");
      });
    },
    clearFocus() {
      if (!this.cy) return;
      this.cy.batch(() => this.cy.elements().removeClass("vdb-dim vdb-focus"));
    },
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
            // a big ministry becomes a taller, larger-typed plaque; width
            // stays label-driven so the name still fits its box
            "font-size": "mapData(roster, 0, 30, 11, 16)",
            "font-family": serif,
            "font-weight": "bold",
            // teams are the wayfinding layer, so their names survive much
            // further out than the volunteers' do
            "min-zoomed-font-size": 4,
            width: "label",
            height: "mapData(roster, 0, 30, 24, 44)",
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
            // ~290 names drawn at once is label soup; they arrive as you zoom
            // into a cluster instead, and hovering reveals them at any zoom
            // (see .vdb-focus at the end of this sheet)
            "min-zoomed-font-size": 8,
            "text-valign": "bottom",
            "text-margin-y": "4px",
            // someone holding five ministries reads as a hub, not as another dot
            width: "mapData(links, 1, 5, 14, 26)",
            height: "mapData(links, 1, 5, 14, 26)",
          },
        },
        {
          // workload colouring; nodes without a color datum keep the neutral above
          selector: 'node[type="volunteer"][color]',
          style: { "background-color": "data(color)" },
        },
        {
          // headshot fills the bubble. Model size stays degree-driven — the
          // canvas renders the photo at screen resolution, so it resolves on zoom.
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
          // the membership mesh recedes so the leadership spine reads through
          // it; every edge is still drawn, just no longer shouting
          selector: "edge",
          style: {
            width: 1,
            "line-color": c.edge,
            opacity: 0.35,
            "curve-style": "haystack",
          },
        },
        {
          selector: "edge[?leadership]",
          style: { width: 2.5, "line-color": c.leader, opacity: 0.9 },
        },
        {
          selector: "edge[?hierarchy]",
          style: {
            width: 2,
            "line-color": c.hier,
            opacity: 0.85,
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
        // last in the sheet on purpose: hover focus outranks selection, so
        // the isolated neighbourhood is never half-overpainted by it
        {
          selector: ".vdb-dim",
          style: { opacity: 0.12, "text-opacity": 0 },
        },
        {
          // 0 = no minimum: the focused set shows its names at any zoom, which
          // is what makes the zoom gate above affordable
          selector: ".vdb-focus",
          style: { "min-zoomed-font-size": 0, "z-index": 10 },
        },
      ];
    },
    layoutOptions(numIter = 300) {
      // cose defaults to numIter 1000; the layout runs synchronously on the
      // main thread, so iterations are paid for in time-to-first-render.
      // The spacing values are wider than cose's defaults: a parish is mostly
      // disconnected islands (66 teams sharing few people), and at the
      // defaults they pile into one another.
      return {
        name: "cose",
        animate: false,
        nodeOverlap: 16,
        idealEdgeLength: 80,
        nodeRepulsion: 4500,
        componentSpacing: 120,
        gravity: 0.6,
        padding: 20,
        numIter,
      };
    },
    refresh(elements) {
      if (!this.cy) return;
      this.clearFocus();
      // carry surviving nodes' positions across the swap: cose keeps
      // randomize:false, so it refines from where nodes already are instead
      // of re-annealing the whole map (and re-scrambling it) on every filter
      const pos = {};
      this.cy.nodes().forEach((n) => {
        pos[n.id()] = { ...n.position() };
      });
      this.cy.elements().remove();
      this.cy.add(this.annotated(elements));
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
