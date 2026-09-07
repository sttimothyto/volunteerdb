"""Admin screen for workload: role multipliers, colour band thresholds, team weights."""

from decimal import Decimal

from nicegui import ui

from .. import query_lang
from ..fp import Err, Ok
from ..models import ROLE_LABELS, TeamRole
from ..services import teams as team_service
from ..services import workload as workload_service
from .a11y import heading
from .context import PageCtx, page_ctx, run_command, success
from .guards import deny_unless_admin
from .layout import frame
from .tables import count_text, wire_search


def _contrast_note(color: ui.color_input) -> None:
    """Beside each band colour: the ratio its badge text will read at. The
    label colour is picked per band (ink or white), and a band no text reads
    on is refused on save — this is the same arithmetic, shown up front."""
    note = ui.label().classes("text-sm text-gray-500 w-36")

    def show() -> None:
        ratio = workload_service.contrast_with_label(color.value or "")
        if ratio is None:
            note.set_text("not a colour")
            return
        floor = workload_service.MIN_LABEL_CONTRAST
        note.set_text(
            f"badge text {ratio:.1f}:1"
            + ("" if ratio >= floor else f" — below {floor}:1")
        )

    color.on_value_change(lambda _: show())
    show()


@ui.page("/admin/workload")
async def workload_page():
    async with page_ctx() as ctx:
        session, actor = ctx.session, ctx.actor
        config = await workload_service.read_config(session)
        tree = await team_service.tree(session)
        all_teams, paths = tree.teams, tree.paths
    if deny_unless_admin(actor, "Workload", help="workload"):
        return

    with frame("Workload", actor, help="workload"):
        ui.label(
            "A volunteer's workload score is the sum, over every team they serve on, of the "
            "team's workload weight × their role's multiplier. Bands colour-code the score on "
            "the volunteers list and the graph. Visible to admins and to the "
            "leaders/seconds of a volunteer's teams; configured here by admins only."
        ).classes("text-sm text-gray-500 vdb-prose")

        with ui.card().classes("w-full gap-2 p-4"):
            heading("Role multipliers", level=2)
            multiplier_inputs: dict[TeamRole, ui.number] = {}
            with ui.row().classes("gap-4"):
                for role in TeamRole:
                    multiplier_inputs[role] = (
                        ui.number(
                            ROLE_LABELS[role],
                            value=float(config.multipliers[role]),
                            min=0,
                            step=0.5,
                        )
                        .props("outlined dense")
                        .classes("w-40")
                    )

            heading("Colour bands", level=2)
            ui.label(
                "Saving recolours the badges on the volunteers list, the dots on "
                "the graph and the workload chips on the dashboard."
            ).classes("text-sm text-gray-500 vdb-prose")
            band_rows: list[tuple[ui.input, ui.color_input, ui.number | None]] = []
            for i, b in enumerate(config.bands):
                is_last = i == len(config.bands) - 1
                with ui.row().classes("items-center gap-3"):
                    label = (
                        ui.input("Label", value=b.label)
                        .props("outlined dense")
                        .classes("w-32")
                    )
                    color = (
                        ui.color_input(label="Colour", value=b.color)
                        .props("dense")
                        .classes("w-36")
                    )
                    _contrast_note(color)
                    if is_last:
                        upper = None
                        ui.label("everything above").classes("text-sm text-gray-500")
                    else:
                        upper = (
                            ui.number(
                                "up to score", value=float(b.upper), min=0, step=0.5
                            )
                            .props("outlined dense")
                            .classes("w-32")
                        )
                    band_rows.append((label, color, upper))

            async def save_config() -> None:
                new_config = workload_service.WorkloadConfig(
                    multipliers={
                        role: Decimal(str(inp.value or 0))
                        for role, inp in multiplier_inputs.items()
                    },
                    bands=[
                        workload_service.Band(
                            (label.value or "").strip(),
                            color.value or "#9e9e9e",
                            None if upper is None else Decimal(str(upper.value or 0)),
                        )
                        for label, color, upper in band_rows
                    ],
                )

                async def command(ctx: PageCtx):
                    return await workload_service.set_config(
                        ctx.session, ctx.actor, new_config, now=ctx.now
                    )

                await run_command(
                    command, reload=False, success="Workload settings saved"
                )

            ui.button("Save settings", icon="save", on_click=save_config).props("dense")

        with ui.card().classes("w-full gap-2 p-4"):
            heading("Team workload weights", level=2)
            ui.label(
                "Optional per-ministry weight; empty teams don't count towards anyone's score. "
                "Also editable on each team's edit dialog."
            ).classes("text-sm text-gray-500 vdb-prose")
            _weights_table(all_teams, paths)


# --- the weights: a table whose rows are the state -----------------------------------
#
# One number per team was a row of widgets per team -- 36 here, 66 at a
# large parish -- with no search and a label pinned at w-96 that a phone
# could not show. The table's rows carry the weight; the input in each
# Weight cell emits the typed value with its row, the handler writes it back
# into the table's rows (the widget is the state, as ever), and Save diffs
# the rows against what the page loaded.

_WEIGHT_CELL = """
<q-td key="weight" :props="props">
    <q-input type="number" dense outlined clearable step="0.5" min="0" class="vdb-weight"
             :model-value="props.row.weight" debounce="300"
             :aria-label="'Weight of ' + props.row.path"
             @update:model-value="v => $parent.$emit('weight', {id: props.row.id, value: v})" />
</q-td>
"""
WEIGHT_COLUMNS = [
    {
        "name": "ministry",
        "label": "Ministry",
        "field": "ministry",
        "align": "left",
        "sortable": True,
    },
    {
        "name": "team",
        "label": "Team",
        "field": "path",
        "align": "left",
        "sortable": True,
    },
    {
        "name": "weight",
        "label": "Weight",
        "field": "weight",
        "align": "left",
        "sortable": True,
    },
]


def _weight_rows(all_teams, paths: dict[int, str]) -> list[dict]:
    """One row per team, grouped by its top-level ministry (the first
    segment of its path), in path order."""
    rows = []
    for team in sorted(all_teams, key=lambda t: paths[t.id].lower()):
        path = paths[team.id]
        rows.append(
            {
                "id": team.id,
                "ministry": path.split(" / ", 1)[0],
                "path": path,
                "weight": float(team.workload_weight),
            }
        )
    return rows


def _matching_teams(rows: list[dict], text: str) -> list[dict]:
    return [r for r in rows if text in r["path"].lower()]


def _weights_table(all_teams, paths: dict[int, str]) -> None:
    with ui.row().classes("items-center gap-2 w-full"):
        search = (
            ui.input("Search teams…")
            .props("outlined dense clearable debounce=200")
            .classes("grow")
            .mark("weights-search")
        )
    table = (
        ui.table(
            columns=WEIGHT_COLUMNS,
            rows=_weight_rows(all_teams, paths),
            row_key="id",
            pagination=0,
        )
        .props("hide-no-data")
        .classes("w-full vdb-weights")
        .mark("weights")
    )
    table.add_slot("body-cell-weight", _WEIGHT_CELL)
    # the table copies the rows it is handed, so its own list is the one
    # state: the search narrows it to subsets of these dicts, a typed weight
    # lands in them, and Save reads them all
    rows = table.rows
    originals = {r["id"]: Decimal(str(r["weight"])) for r in rows}

    def typed(e) -> None:
        """A typed weight lands in the table's rows: the widget is the state."""
        row = next((r for r in rows if r["id"] == e.args.get("id")), None)
        if row is None:
            return
        value = e.args.get("value")
        try:
            row["weight"] = float(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return

    table.on("weight", typed)
    count = ui.label(count_text(len(rows), None, "team")).classes(
        "text-sm text-gray-500"
    )
    wire_search(
        search,
        count,
        table,
        rows,
        noun="team",
        compile=query_lang.compile_weights,
        text_filter=_matching_teams,
    )

    async def save_weights() -> None:
        async def command(ctx: PageCtx):
            changed = 0
            for row in rows:
                # a cleared box is weight 0, which is what excluding a
                # ministry from the scores has always meant
                new = Decimal(str(row["weight"] if row["weight"] is not None else 0))
                if new != originals[row["id"]]:
                    put = await team_service.update(
                        ctx.session, ctx.actor, row["id"], workload_weight=new
                    )
                    if isinstance(put, Err):
                        return put
                    changed += 1
            return Ok(changed)

        await run_command(
            command,
            on_ok=lambda changed, _e, _r: success(
                f"Updated {changed} team weight{'s' if changed != 1 else ''}"
            ),
            reload=False,
        )

    # sticky at the foot of the card: the button is in reach at any scroll
    with ui.row().classes("w-full justify-end vdb-sticky-actions"):
        ui.button("Save weights", icon="save", on_click=save_weights).props("dense")
