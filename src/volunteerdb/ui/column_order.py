"""Reader-chosen column order for the big listings, held for the session.

Quasar renders columns in the order the `columns` list arrives in, so the whole
feature is one permutation applied before ui.table() is built, plus a
header-cell slot that makes each <th> draggable and a delegated listener
(static/column_drag.js) that reports the drop back over the socket. No column
definition is edited: the same dicts, in a different order.

What is saved is a list of column *names*, never indices. The column set is not
fixed -- /teams hides the coverage counts from a plain member, and /volunteers
grows one cf_<key> column per custom field an admin marks show_in_list -- so an
index saved on one visit would mean a different column on the next.

The order lives in app.storage.user and is dropped by context.clear_session(),
so it survives reloads and navigation but not signing out.
"""

from collections.abc import Sequence

from nicegui import app, ui

from .assets import static_url

STORAGE_KEY = "column_order"  # app.storage.user -> {page key: [column name, ...]}
FIXED = "vdbFixed"  # column-dict flag: this one never moves (Quasar ignores it)

# One <th> per column, replacing Quasar's default. `:props="props"` is what
# keeps click-to-sort and the sort arrow: QTh renders a bare <th> when `props`
# is undefined, and reads props.col / props.sort out of the header-cell scope
# when it is set. The two markers are all the JS layer needs; a pinned column
# gets neither, so it is neither a drag source nor a drop target.
HEADER_CELL = """
<q-th :props="props"
      :draggable="!props.col.vdbFixed"
      :data-vdb-col="props.col.vdbFixed ? null : props.col.name">
    {{ props.col.label }}
</q-th>
"""


def reorder(order: Sequence[str], moved: str, target: str) -> list[str]:
    """`moved` takes `target`'s slot; everything in between shuffles along.

    Insert, not swap: dragging Status onto Name should walk Status to the front,
    not fling Name to the far right -- that is what every table the reader has
    ever used does, and a swap makes a long drag feel like a mistake.

    Taking the target's index *before* removing `moved` makes the side fall out
    of the drag direction with no extra branch: rightwards lands after the
    target, leftwards before it, and an adjacent drag is a swap either way.

    Anything unrecognised returns the order unchanged: both arguments arrive
    over the socket and are worth exactly as much trust as that implies.
    """
    out = list(order)
    if moved == target or moved not in out or target not in out:
        return out
    at = out.index(target)
    out.remove(moved)
    out.insert(at, moved)
    return out


def merge(saved: Sequence[str], present: Sequence[str]) -> list[str]:
    """`present`, in the reader's saved order.

    Two things a saved order is never allowed to do: hide a column that exists
    now (a custom field added since the last visit must still show up), or
    resurrect one that does not (a field deleted, or the coverage counts after a
    demotion). So this sorts `present` rather than filtering `saved` -- a name
    absent from `saved` keeps the neighbour the page declared it after, and a
    name absent from `present` simply never comes up.

    With nothing saved this is the identity, which is what keeps each page's
    declared order the default.
    """
    rank = {name: i for i, name in enumerate(saved)}
    keys: dict[str, tuple[int, int]] = {}
    anchor = -1  # rank of the nearest saved column to the left, in page order
    for i, name in enumerate(present):
        if name in rank:
            anchor = rank[name]
            keys[name] = (anchor, 0)
        else:
            keys[name] = (anchor, i + 1)  # trails the column it was declared after
    return sorted(present, key=lambda name: keys[name])


def apply_order(saved: Sequence[str], columns: list[dict]) -> list[dict]:
    """`columns` permuted into `saved`, with FIXED columns held in place.

    The fixed ones keep their absolute index and the rest fill the slots that
    are left, so a pinned first column stays first however the others move.
    """
    by_name = {c["name"]: c for c in columns}
    movable = [c["name"] for c in columns if not c.get(FIXED)]
    wanted = iter(merge(saved, movable))
    return [c if c.get(FIXED) else by_name[next(wanted)] for c in columns]


def saved_order(key: str) -> list[str]:
    """The names this browser last arranged for `key`. Total by construction:
    the store is plain JSON that an older release could have written."""
    store = app.storage.user.get(STORAGE_KEY)
    names = store.get(key) if isinstance(store, dict) else None
    if not isinstance(names, list):
        return []
    return [n for n in names if isinstance(n, str)]


def _save(key: str, names: Sequence[str]) -> None:
    # whole-key assignment rather than store[key] = ...: a top-level write is
    # what the storage dict always notices, with no reliance on nested observation
    store = dict(app.storage.user.get(STORAGE_KEY) or {})
    store[key] = list(names)
    app.storage.user[STORAGE_KEY] = store


def apply_saved_order(key: str, columns: list[dict]) -> list[dict]:
    """Call on the list you are about to hand to ui.table()."""
    return apply_order(saved_order(key), columns)


def make_draggable(table: ui.table, key: str) -> None:
    """Let the reader drag this table's headers around, remembering the result
    under `key` until the session ends (see context.clear_session)."""
    # page-scoped like the dashboard's cytoscape preload, not shared like
    # theme.css: two of fifteen pages want it, and the file guards itself
    # against a second injection
    ui.add_head_html(f'<script defer src="{static_url("column_drag.js")}"></script>')
    table.add_slot("header-cell", HEADER_CELL)

    def move(e) -> None:
        args = e.args if isinstance(e.args, dict) else {}
        moved, target = args.get("moved"), args.get("target")
        if not isinstance(moved, str) or not isinstance(target, str):
            return
        names = [c["name"] for c in table.columns if not c.get(FIXED)]
        wanted = reorder(names, moved, target)
        if wanted == names:  # drop on self, or a name this table does not have
            return
        _save(key, wanted)
        table.columns = apply_order(wanted, table.columns)
        table.update()  # assigning the property only stages it (cf. _wire_search)

    # camelCase deliberately: NiceGUI camel-cases the type for the browser, so
    # this string, the $emit in column_drag.js and the .trigger() in the tests
    # are all the same string with no translation step in between.
    table.on("vdbColMove", move, args=["moved", "target"])
