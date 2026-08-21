"""The parish's own logo: normalization and storage.

One image for the whole instance, uploaded by an admin, shown in the app
header, above the login box, and on the public ministries shell. Stored in
Postgres like every other binary here (services/photos.py, TeamPageImage) —
there is no object store, and ui/static is baked into the container image.

normalize() deliberately differs from photos.normalize in two ways, because a
logo is not a headshot:

* **Alpha is preserved, never flattened onto white.** The app header is
  terracotta (#a5573e); a logo composited onto white would render as a white
  box sitting on it.
* **Aspect ratio is preserved** (ImageOps.contain, not ImageOps.fit). A
  wordmark is usually wide, and centre-cropping it to a square would cut the
  parish's name in half.
"""

from io import BytesIO

import sqlalchemy as sa
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import SiteLogo
from ..permissions import Actor, require

# Big enough to stay crisp on a retina login page (rendered at 64px) without
# storing a print asset; the row is read on every uncached /logo request.
LOGO_BOX = 512
LOGO_MAX_BYTES = 250_000
MAX_UPLOAD_BYTES = 10_000_000
CONTENT_TYPE = "image/png"
# The one row. `id` is pinned by a CHECK constraint, so an upload upserts.
ROW_ID = 1


def normalize(data: bytes) -> bytes:
    """Sync and CPU-bound — call via anyio.to_thread.run_sync from handlers."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("image file larger than 10 MB")
    try:
        img = Image.open(BytesIO(data))
        img = ImageOps.exif_transpose(img)
        # RGBA throughout: a logo with a transparent ground has to keep it
        img = img.convert("RGBA")
        # contain, not fit: shrink to fit the box, never crop, never upscale
        img.thumbnail((LOGO_BOX, LOGO_BOX), Image.Resampling.LANCZOS)
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        OSError,
        ValueError,
    ) as exc:
        raise ValueError("not a readable image") from exc
    buffer = BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    encoded = buffer.getvalue()
    if len(encoded) > LOGO_MAX_BYTES:
        raise ValueError(
            "logo is too detailed to store — try a flatter image, or one with "
            "fewer colours"
        )
    return encoded


async def get(session: AsyncSession) -> SiteLogo | None:
    return await session.get(SiteLogo, ROW_ID)


async def set_logo(
    session: AsyncSession,
    actor: Actor | None,
    image: bytes,
    *,
    normalized: bool = False,
) -> None:
    """Replace the site logo. `normalized=True` is the web dialog saying it
    already ran normalize() to build its preview, so the bytes shown are
    exactly the bytes stored."""
    require(actor is None or actor.is_admin, "set the site logo")
    stored = image if normalized else normalize(image)
    stmt = pg_insert(SiteLogo).values(
        id=ROW_ID,
        image=stored,
        content_type=CONTENT_TYPE,
        uploaded_by=actor.user.id if actor is not None else None,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[SiteLogo.id],
        set_={
            "image": stmt.excluded.image,
            "content_type": stmt.excluded.content_type,
            "uploaded_by": stmt.excluded.uploaded_by,
            "uploaded_at": sa.func.now(),
        },
    )
    await session.execute(stmt)


async def delete_logo(session: AsyncSession, actor: Actor | None) -> None:
    """Back to the shipped placeholder (ui/static/logo-placeholder.svg)."""
    require(actor is None or actor.is_admin, "set the site logo")
    await session.execute(sa.delete(SiteLogo).where(SiteLogo.id == ROW_ID))
