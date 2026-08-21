"""Site logo: the header widget and the admin upload dialog.

Modelled on photo_dialog.py, with the same shape for the same reasons — the
picked file is normalized immediately, so a bad file fails before Save and the
preview shows exactly what will be stored. The differences are who may use it
(admins only, where a headshot is open to everyone) and that there is no legal
declaration: this is the parish's own mark, not a photograph of a person.
"""

import base64
from collections.abc import Awaitable, Callable

import anyio.to_thread
from nicegui import events, ui

from ..permissions import Actor
from ..services import branding
from .context import action_session, notify_errors

# /logo serves the uploaded image or the shipped placeholder, so the src never
# has to be decided at render time (ui/logo_route.py explains why it must not).
LOGO_URL = "/logo"


def logo_img(src: str, classes: str) -> ui.element:
    """A plain <img>, deliberately not ui.image.

    ui.image renders Quasar's QImg, which sizes itself from a padding-ratio box
    and collapses to zero width under `w-auto` — so the logo silently did not
    render at all. A bare <img> takes its width from the file's own aspect
    ratio, which is what a mark of unknown proportions needs."""
    return ui.element("img").props(f'src="{src}" alt=""').classes(classes)


def open_logo_dialog(on_change: Callable[[], Awaitable[None]]) -> None:
    state: dict = {"image": None}
    with ui.dialog() as dialog, ui.card().classes("w-96 gap-3"):
        ui.label("Site logo").classes("text-lg font-medium")
        ui.label(
            "Shown in this header, above the login box, and on the public "
            "ministries pages. A wide wordmark is fine — the image is scaled "
            "to fit, never cropped, and transparency is kept."
        ).classes("text-sm text-gray-500")
        preview = logo_img(LOGO_URL, "h-24 w-auto self-center object-contain")

        @notify_errors
        async def on_upload(e: events.UploadEventArguments) -> None:
            raw = await e.file.read()
            state["image"] = await anyio.to_thread.run_sync(branding.normalize, raw)
            data_url = "data:image/png;base64," + base64.b64encode(
                state["image"]
            ).decode("ascii")
            preview.props(f'src="{data_url}"')
            preview.update()

        ui.upload(
            label="Drop a logo here (stored as PNG, at most 512×512)",
            on_upload=on_upload,
            auto_upload=True,
            max_file_size=branding.MAX_UPLOAD_BYTES,
        ).props('accept="image/*" max-files=1').classes("w-full")

        @notify_errors
        async def save() -> None:
            if state["image"] is None:
                ui.notify("Choose an image first", color="warning")
                return
            async with action_session() as (session, actor):
                await branding.set_logo(session, actor, state["image"], normalized=True)
            dialog.close()
            ui.notify("Logo saved", color="positive")
            await on_change()

        @notify_errors
        async def remove() -> None:
            async with action_session() as (session, actor):
                await branding.delete_logo(session, actor)
            dialog.close()
            ui.notify("Logo removed", color="positive")
            await on_change()

        with ui.row().classes("justify-end w-full gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Remove logo", on_click=remove).props("flat color=negative")
            ui.button("Upload", on_click=save)
    dialog.open()


def site_logo(actor: Actor, *, classes: str, marker: str = "site-logo") -> None:
    """The logo itself. Admins can click it to replace it — the same
    click-the-image-to-change-it idiom as photo_dialog.photo_avatar, and the
    only way in: a site-wide logo does not earn a settings page of its own."""
    element = logo_img(LOGO_URL, f"{classes} object-contain")
    if not actor.is_admin:
        return

    async def changed() -> None:
        # a full reload, so the header, and anything else showing it, refresh
        ui.navigate.reload()

    # an <img> has no on_click parameter; the generic .on() is the idiom
    element.mark(marker).classes("cursor-pointer").tooltip("Change the site logo").on(
        "click", lambda: open_logo_dialog(changed)
    )
