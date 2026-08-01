"""Admin screen for defining custom volunteer fields."""

from nicegui import ui

from ..models import FIELD_TYPE_LABELS, FieldType
from ..permissions import require
from ..services import custom_fields as custom_field_service
from .context import action_session, notify_errors, page_session
from .layout import frame

TYPE_OPTIONS = {ft.value: FIELD_TYPE_LABELS[ft] for ft in FieldType}


@ui.page("/admin/fields")
async def fields_page():
    async with page_session() as (session, actor):
        if not actor.is_admin:
            with frame("Custom fields", actor):
                ui.label("Admins only.").classes("text-gray-500")
            return
        defs = await custom_field_service.list_defs(session, include_inactive=True)

    with frame("Custom fields", actor):
        ui.label(
            "Extra volunteer properties. Values are edited on each volunteer's page and "
            "are visible to whoever may see that volunteer's contact details."
        ).classes("text-sm text-gray-500")
        ui.button("New field", icon="add", on_click=lambda: _field_dialog()).props(
            "dense"
        )

        if not defs:
            ui.label("No custom fields defined yet.").classes("text-gray-500")
        for defn in defs:
            with ui.row().classes(
                "w-full items-center gap-3 p-2 rounded hover:bg-gray-100"
            ):
                ui.label(defn.label).classes("font-medium w-56")
                ui.badge(TYPE_OPTIONS.get(defn.field_type, defn.field_type))
                if defn.options:
                    ui.label(", ".join(defn.options)).classes("text-sm text-gray-500")
                if defn.show_in_list:
                    ui.badge("in list", color="info")
                if not defn.is_active:
                    ui.badge("inactive", color="grey")
                ui.space()
                ui.button(
                    icon="edit", on_click=lambda _, d=defn: _field_dialog(d)
                ).props("dense flat")
                ui.button(
                    icon="delete", on_click=lambda _, d=defn: _delete_dialog(d)
                ).props("dense flat color=negative")


def _field_dialog(defn=None) -> None:
    """Create (defn=None) or edit a field definition. Type and key are immutable."""
    with ui.dialog() as dialog, ui.card().classes("w-96 gap-3"):
        ui.label("Edit field" if defn else "New field").classes("text-lg font-medium")
        label = (
            ui.input("Label", value=defn.label if defn else "")
            .props("outlined dense")
            .classes("w-full")
        )
        if defn is None:
            field_type = (
                ui.select(TYPE_OPTIONS, label="Type", value=FieldType.text.value)
                .props("outlined dense")
                .classes("w-full")
            )
        else:
            field_type = None
            ui.label(
                f"Type: {TYPE_OPTIONS.get(defn.field_type, defn.field_type)} — key: {defn.key}"
            ).classes("text-sm text-gray-500")
        options = (
            ui.textarea(
                "Options (one per line)",
                value="\n".join(defn.options or []) if defn else "",
            )
            .props("outlined dense")
            .classes("w-full")
        )
        if defn is None:
            options.bind_visibility_from(
                field_type, "value", lambda v: v == FieldType.select.value
            )
        else:
            options.set_visibility(defn.field_type == FieldType.select.value)
        show_in_list = ui.switch(
            "Show as a column on the volunteers list",
            value=defn.show_in_list if defn else False,
        )
        position = (
            ui.number("Sort position", value=defn.position if defn else 0, precision=0)
            .props("outlined dense")
            .classes("w-32")
        )
        active = ui.switch("Active", value=defn.is_active) if defn else None

        @notify_errors
        async def save() -> None:
            option_list = [
                line for line in (options.value or "").splitlines() if line.strip()
            ]
            async with action_session() as (session, actor):
                require(actor.is_admin, "only admins define custom fields")
                if defn is None:
                    await custom_field_service.create_def(
                        session,
                        label.value,
                        field_type.value,
                        options=option_list,
                        show_in_list=show_in_list.value,
                        position=int(position.value or 0),
                    )
                else:
                    is_select = defn.field_type == FieldType.select.value
                    await custom_field_service.update_def(
                        session,
                        defn.id,
                        label=label.value,
                        show_in_list=show_in_list.value,
                        position=int(position.value or 0),
                        is_active=active.value,
                        **({"options": option_list} if is_select else {}),
                    )
            dialog.close()
            ui.navigate.reload()

        with ui.row().classes("justify-end w-full gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Save", on_click=save)
    dialog.open()


def _delete_dialog(defn) -> None:
    with ui.dialog() as dialog, ui.card().classes("gap-3"):
        ui.label(f"Delete the field “{defn.label}”?").classes("font-medium")
        ui.label(
            "Stored values stay in volunteer history but will no longer be shown. "
            "Consider deactivating instead if you may want it back."
        ).classes("text-sm text-gray-500")

        @notify_errors
        async def confirm() -> None:
            async with action_session() as (session, actor):
                require(actor.is_admin, "only admins delete custom fields")
                await custom_field_service.delete_def(session, defn.id)
            dialog.close()
            ui.navigate.reload()

        with ui.row().classes("justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Delete", on_click=confirm).props("color=negative")
    dialog.open()
