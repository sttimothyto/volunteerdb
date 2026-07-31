"""Volunteer headshots: normalization and storage.

Every stored photo is the output of normalize(): EXIF-transposed, flattened
onto white, center-cropped square, 400x400 JPEG no larger than
PHOTO_MAX_BYTES. Uploads via the web dialog, the API, and the spreadsheet
importer all funnel through it, so exports can base64 the stored bytes as-is
and round-trips are byte-identical.
"""

from collections.abc import Iterable
from datetime import UTC, datetime
from io import BytesIO

import anyio.to_thread
import sqlalchemy as sa
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Volunteer, VolunteerPhoto

PHOTO_SIZE = 400
# Excel caps a cell at 32,767 chars and base64 takes 4*ceil(n/3) chars, so the
# exported cell fits iff the stored image is <= 24,573 bytes; 24,000 for margin.
PHOTO_MAX_BYTES = 24_000
MAX_UPLOAD_BYTES = 10_000_000
_QUALITY_STEPS = (85, 75, 65, 55, 45, 35)


def normalize(data: bytes) -> bytes:
    """Sync and CPU-bound — call via anyio.to_thread.run_sync from handlers."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("image file larger than 10 MB")
    try:
        img = Image.open(BytesIO(data))
        img = ImageOps.exif_transpose(img)
        if img.mode != "RGB":
            rgba = img.convert("RGBA")
            white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            img = Image.alpha_composite(white, rgba).convert("RGB")
        img = ImageOps.fit(img, (PHOTO_SIZE, PHOTO_SIZE), Image.Resampling.LANCZOS)
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
        raise ValueError("not a readable image") from exc
    for quality in _QUALITY_STEPS:
        buffer = BytesIO()
        img.save(buffer, format="JPEG", quality=quality, optimize=True)
        encoded = buffer.getvalue()
        if len(encoded) <= PHOTO_MAX_BYTES:
            return encoded
    raise ValueError("image does not compress to a storable size")


async def get(session: AsyncSession, volunteer_id: int) -> VolunteerPhoto | None:
    return await session.get(VolunteerPhoto, volunteer_id)


async def set_photo(
    session: AsyncSession,
    volunteer_id: int,
    data: bytes,
    *,
    uploaded_by: int | None,
    normalized: bool = False,
) -> VolunteerPhoto:
    if await session.get(Volunteer, volunteer_id) is None:
        raise LookupError(f"volunteer {volunteer_id} not found")
    image = data if normalized else await anyio.to_thread.run_sync(normalize, data)
    photo = await session.get(VolunteerPhoto, volunteer_id)
    # uploaded_at is set app-side (not left to onupdate) because it feeds the
    # ?v= cache-buster: a replace must always move it
    if photo is None:
        photo = VolunteerPhoto(
            volunteer_id=volunteer_id,
            image=image,
            uploaded_by=uploaded_by,
            uploaded_at=datetime.now(UTC),
        )
        session.add(photo)
    else:
        photo.image = image
        photo.uploaded_by = uploaded_by
        photo.uploaded_at = datetime.now(UTC)
    await session.flush()
    return photo


async def delete_photo(session: AsyncSession, volunteer_id: int) -> None:
    """Idempotent: no photo is fine; only an unknown volunteer is an error."""
    if await session.get(Volunteer, volunteer_id) is None:
        raise LookupError(f"volunteer {volunteer_id} not found")
    photo = await session.get(VolunteerPhoto, volunteer_id)
    if photo is not None:
        await session.delete(photo)
        await session.flush()


async def versions(
    session: AsyncSession, volunteer_ids: Iterable[int] | None = None
) -> dict[int, datetime]:
    """volunteer_id -> uploaded_at for cache-busted URLs; never loads the blob."""
    stmt = sa.select(VolunteerPhoto.volunteer_id, VolunteerPhoto.uploaded_at)
    if volunteer_ids is not None:
        ids = list(volunteer_ids)
        if not ids:
            return {}
        stmt = stmt.where(VolunteerPhoto.volunteer_id.in_(ids))
    return dict((await session.execute(stmt)).all())  # type: ignore[arg-type]


async def images(session: AsyncSession, volunteer_ids: Iterable[int]) -> dict[int, bytes]:
    """Blobs, for the spreadsheet exporter only."""
    ids = list(volunteer_ids)
    if not ids:
        return {}
    stmt = sa.select(VolunteerPhoto.volunteer_id, VolunteerPhoto.image).where(
        VolunteerPhoto.volunteer_id.in_(ids)
    )
    return dict((await session.execute(stmt)).all())  # type: ignore[arg-type]


def photo_url(volunteer_id: int, uploaded_at: datetime) -> str:
    """Cookie-authenticated image URL; v= makes replacements bust browser caches.
    Millisecond resolution so a same-second replacement still changes the URL."""
    return f"/photos/{volunteer_id}?v={int(uploaded_at.timestamp() * 1000)}"
