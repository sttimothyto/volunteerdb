"""Headshot upload dialog, shared by the volunteer panel and the detail page.

Uploading is open to every signed-in account by design (deliberate exception
to can_edit_volunteer). The picked file is normalized immediately — bad files
fail before Save, and the preview shows exactly what will be stored — but
nothing touches the database until Upload is clicked, which the legal
declaration checkbox gates.
"""

import base64
from collections.abc import Awaitable, Callable
from datetime import datetime

import anyio.to_thread
from nicegui import events, ui

from ..services import photos as photo_service
from .context import action_session, notify_errors

DISCLAIMER = (
    "I confirm this is an appropriate professional photo. "
    "Illegal or explicit content will be reported to the authorities."
)


def open_photo_dialog(
    volunteer_id: int,
    full_name: str,
    photo_at: datetime | None,
    on_change: Callable[[], Awaitable[None]],
) -> None:
    state: dict = {"image": None}
    with ui.dialog() as dialog, ui.card().classes("w-96 gap-3"):
        ui.label(f"Photo — {full_name}").classes("text-lg font-medium")
        # current photo until a file is picked, then the normalized preview
        preview = ui.image().classes("w-40 h-40 rounded-full object-cover self-center")
        if photo_at is not None:
            preview.set_source(photo_service.photo_url(volunteer_id, photo_at))
        else:
            preview.classes("hidden")

        @notify_errors
        async def on_upload(e: events.UploadEventArguments) -> None:
            raw = await e.file.read()
            state["image"] = await anyio.to_thread.run_sync(
                photo_service.normalize, raw
            )
            preview.set_source(
                "data:image/jpeg;base64,"
                + base64.b64encode(state["image"]).decode("ascii")
            )
            preview.classes(remove="hidden")

        ui.upload(
            label="Drop a headshot here (stored as 400×400 JPEG)",
            on_upload=on_upload,
            auto_upload=True,
            max_file_size=photo_service.MAX_UPLOAD_BYTES,
        ).props('accept="image/*" max-files=1').classes("w-full")

        agree = ui.checkbox(DISCLAIMER).classes("text-sm")

        @notify_errors
        async def save() -> None:
            if state["image"] is None:
                ui.notify("Choose a photo first", color="warning")
                return
            if not agree.value:
                ui.notify("Please confirm the declaration first", color="warning")
                return
            async with action_session() as (session, actor):
                await photo_service.set_photo(
                    session,
                    volunteer_id,
                    state["image"],
                    uploaded_by=actor.user.id,
                    normalized=True,
                )
            dialog.close()
            ui.notify("Photo saved", color="positive")
            await on_change()

        @notify_errors
        async def remove() -> None:
            async with action_session() as (session, _actor):
                await photo_service.delete_photo(session, volunteer_id)
            dialog.close()
            ui.notify("Photo removed", color="positive")
            await on_change()

        with ui.row().classes("justify-end w-full gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            if photo_at is not None:
                ui.button("Remove photo", on_click=remove).props("flat color=negative")
            ui.button("Upload", on_click=save).bind_enabled_from(agree, "value")
    dialog.open()


def photo_avatar(
    volunteer_id: int,
    full_name: str,
    photo_at: datetime | None,
    on_change: Callable[[], Awaitable[None]] | None,
) -> None:
    """Round headshot (or the person icon) for a header row; clickable to open
    the dialog unless on_change is None (read-only as-of views)."""
    if photo_at is not None:
        element = ui.image(photo_service.photo_url(volunteer_id, photo_at)).classes(
            "w-9 h-9 rounded-full object-cover"
        )
    else:
        element = ui.icon("person").classes("text-2xl")
    if on_change is not None:
        # ui.icon/ui.image have no on_click param; the generic .on() is the idiom
        element.mark("photo-avatar").classes("cursor-pointer").tooltip(
            "Add or change photo"
        ).on(
            "click",
            lambda: open_photo_dialog(volunteer_id, full_name, photo_at, on_change),
        )
