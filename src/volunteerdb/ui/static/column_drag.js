/* Drag-to-reorder table column headers. See ui/column_order.py for the server
 * half; this file only reports the gesture.
 *
 * Delegated on `document` on purpose: Quasar re-renders the whole header row on
 * every sort, every table.update() and every /teams search keystroke, so any
 * listener bound to a <th> would be thrown away mid-session. The header cells
 * carry no listeners at all -- only the two markers the header-cell slot puts
 * on them, `draggable` and `data-vdb-col`. A pinned column has neither, so it
 * is silently excluded as both source and target.
 *
 * Touch is not supported: HTML5 dragstart does not fire from touch input, so on
 * a phone the headers stay tappable-to-sort and nothing else happens. A fix
 * would live entirely in this file, emitting the same payload.
 */
(() => {
  "use strict";
  if (window.__vdbColumnDrag) return; // idempotent: the tag is injected per page
  window.__vdbColumnDrag = true;

  const SEL = "th[data-vdb-col]";
  let source = null; // {tableId, name} while a drag is in flight

  const cell = (node) => (node instanceof Element ? node.closest(SEL) : null);

  // The nearest NiceGUI element ancestor of a header cell is its ui.table:
  // every element is rendered with id="c<id>", and nothing between the <th>
  // and the QTable root is a NiceGUI element.
  const tableIdOf = (el) => {
    for (let n = el; n; n = n.parentElement) {
      if (/^c\d+$/.test(n.id)) return Number(n.id.slice(1));
    }
    return null;
  };

  const unmark = (attr) =>
    document.querySelectorAll("[" + attr + "]").forEach((n) => n.removeAttribute(attr));

  const clear = () => {
    unmark("data-vdb-drag");
    unmark("data-vdb-drop");
  };

  document.addEventListener("dragstart", (e) => {
    const th = cell(e.target);
    if (!th) return;
    const tableId = tableIdOf(th);
    if (tableId === null) return;
    source = { tableId, name: th.dataset.vdbCol };
    e.dataTransfer.setData("text/plain", source.name); // Firefox needs a payload
    e.dataTransfer.effectAllowed = "move";
    th.setAttribute("data-vdb-drag", "");
  });

  document.addEventListener("dragover", (e) => {
    const th = cell(e.target);
    if (!source || !th) return;
    if (tableIdOf(th) !== source.tableId) return; // never across two tables
    if (th.dataset.vdbCol === source.name) return; // drop on self: not a target
    e.preventDefault(); // this is what permits the drop
    e.dataTransfer.dropEffect = "move";
    if (!th.hasAttribute("data-vdb-drop")) {
      unmark("data-vdb-drop");
      th.setAttribute("data-vdb-drop", "");
    }
  });

  document.addEventListener("dragleave", (e) => {
    const th = cell(e.target);
    // dragleave also fires stepping between children of the same cell
    if (!th || (e.relatedTarget instanceof Node && th.contains(e.relatedTarget))) return;
    th.removeAttribute("data-vdb-drop");
  });

  document.addEventListener("drop", (e) => {
    const th = cell(e.target);
    if (!source || !th) return;
    e.preventDefault(); // otherwise the browser navigates to the dropped text
    const { tableId, name } = source;
    const target = th.dataset.vdbCol;
    clear();
    source = null;
    if (target === name || tableIdOf(th) !== tableId) return;
    if (typeof getElement !== "function") return;
    getElement(tableId)?.$emit("vdbColMove", { moved: name, target });
  });

  document.addEventListener("dragend", clear); // fires even on a cancelled drag
})();
