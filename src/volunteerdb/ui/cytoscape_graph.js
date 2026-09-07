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
      // laid out just below, once there is an instance to settle() against
      layout: { name: "null" },
      wheelSensitivity: 0.2,
    });
    this.layOut(300, true);
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
    layoutOptions(numIter = 300, randomize = false) {
      // cose defaults to numIter 1000; the layout runs synchronously on the
      // main thread, so iterations are paid for in time-to-first-render
      // (about 2 s for 500 people at 300). The spacing values are several
      // times cose's defaults (idealEdgeLength 32, nodeRepulsion 2048,
      // nodeOverlap 4): a parish is mostly disconnected islands (66 teams
      // sharing few people), and where the teams do share people the plaques
      // are hubs that the defaults pile into one another in the middle of
      // the map. Repulsion in cose acts on the gap between node borders, so
      // a wide plaque pushes its neighbours further than a dot does — but
      // cose caps each node's move at its cooling temperature, so whatever
      // overlap it still has once it has cooled it can no longer resolve;
      // that is separate()'s job.
      return {
        name: "cose",
        animate: false,
        // layOut() fits after settle(), not before it
        fit: false,
        randomize,
        nodeOverlap: 40,
        idealEdgeLength: 120,
        nodeRepulsion: 20000,
        componentSpacing: 120,
        gravity: 0.25,
        numIter,
      };
    },
    // The force layout, then made to fit the window it is drawn in and made
    // overlap-free, then brought into view. `randomize` scatters the nodes
    // across the container before the first pass; a refresh keeps the
    // positions it carried over and refines them instead.
    layOut(numIter, randomize) {
      this.cy.layout(this.layoutOptions(numIter, randomize)).run();
      this.settle();
      this.cy.fit(undefined, 30);
    },
    settle() {
      const cy = this.cy;
      const w = cy.width();
      const h = cy.height();
      const bb = cy.elements().boundingBox({ includeLabels: false });
      if (w > 0 && h > 0 && bb.w > 1 && bb.h > 1) {
        // cose leaves a roughly round graph, and the container is a wide
        // band, so as much as half its width sat empty and the fit zoom was
        // set by the graph's height alone. Stretch one axis and squeeze the
        // other by the same factor: the graph keeps its area and takes the
        // container's aspect. Clamped, so three nodes in a line cannot be
        // flattened into one; derived from the extent each time, so a
        // refresh starting from already stretched positions is left alone
        // rather than stretched again.
        const k = Math.max(0.6, Math.min(1.6, Math.sqrt(w / h / (bb.w / bb.h))));
        if (Math.abs(k - 1) > 0.02) {
          const cx = (bb.x1 + bb.x2) / 2;
          const cy0 = (bb.y1 + bb.y2) / 2;
          cy.nodes().positions((n) => {
            const p = n.position();
            return { x: cx + (p.x - cx) * k, y: cy0 + (p.y - cy0) / k };
          });
        }
      }
      this.separate(8, 300);
    },
    // Push overlapping nodes apart until none overlap. Pairs are compared on
    // their body boxes (a name under a dot is hidden at overview zoom and
    // arrives on hover, so it claims no room) widened by `pad` on every
    // side; each collision moves both nodes along whichever axis frees them
    // with the smaller move. Sweeping in x order keeps a pass cheap enough
    // that 300 of them cost well under a tenth of a second at 500 nodes.
    // The hard requirement — no two bodies overlapping — is met within the
    // first fifty or so; the padding is a wish, and in the dense middle of
    // the map some pairs stay closer than that when the passes run out.
    separate(pad, maxIter) {
      const ns = this.cy.nodes().map((n) => ({
        n,
        x: n.position("x"),
        y: n.position("y"),
        w: n.outerWidth() + 2 * pad,
        h: n.outerHeight() + 2 * pad,
      }));
      const widest = ns.reduce((m, e) => Math.max(m, e.w), 0);
      for (let iter = 0; iter < maxIter; iter++) {
        ns.sort((a, b) => a.x - b.x);
        let moved = 0;
        for (let i = 0; i < ns.length; i++) {
          const a = ns[i];
          for (let j = i + 1; j < ns.length; j++) {
            const b = ns[j];
            if (b.x - a.x >= (a.w + widest) / 2) break;
            const ox = (a.w + b.w) / 2 - Math.abs(a.x - b.x);
            if (ox <= 0) continue;
            const oy = (a.h + b.h) / 2 - Math.abs(a.y - b.y);
            if (oy <= 0) continue;
            if (ox < oy) {
              const s = a.x <= b.x ? 1 : -1;
              a.x -= (s * ox) / 2;
              b.x += (s * ox) / 2;
            } else {
              const s = a.y <= b.y ? 1 : -1;
              a.y -= (s * oy) / 2;
              b.y += (s * oy) / 2;
            }
            moved += 1;
          }
        }
        if (!moved) break;
      }
      this.cy.batch(() => ns.forEach((e) => e.n.position({ x: e.x, y: e.y })));
    },
    refresh(elements) {
      if (!this.cy) return;
      this.clearFocus();
      // carry surviving nodes' positions across the swap: the layout runs
      // without randomize, so it refines from where nodes already are
      // instead of re-annealing the whole map (and re-scrambling it) on
      // every filter
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
      this.layOut(fresh ? 300 : 100, false);
    },
    fit() {
      if (this.cy) this.cy.fit(undefined, 30);
    },
  },
};
