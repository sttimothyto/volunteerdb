"""Headshot service: normalization invariants and storage semantics."""

from datetime import UTC, datetime
from io import BytesIO

import sqlalchemy as sa
from PIL import Image

from volunteerdb import errors
from volunteerdb.db import db_session
from volunteerdb.models import VolunteerPhoto
from volunteerdb.services import photos, volunteers

from tests.fp_helpers import ok, refused


def _png(width: int, height: int, mode: str = "RGBA") -> bytes:
    buffer = BytesIO()
    Image.new(
        mode, (width, height), (200, 120, 80, 255) if mode == "RGBA" else (200, 120, 80)
    ).save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg_with_orientation(width: int, height: int) -> bytes:
    image = Image.new("RGB", (width, height), (10, 200, 30))
    exif = Image.Exif()
    exif[274] = 6  # orientation: rotate 90 CW
    buffer = BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


def test_normalize_produces_square_jpeg_within_cap():
    out = ok(photos.normalize(_png(800, 500)))
    image = Image.open(BytesIO(out))
    assert image.format == "JPEG"
    assert image.size == (photos.PHOTO_SIZE, photos.PHOTO_SIZE), (
        "non-square input center-cropped"
    )
    assert image.mode == "RGB", "alpha flattened"
    assert len(out) <= photos.PHOTO_MAX_BYTES, "base64 must fit an Excel cell"


def test_normalize_strips_exif_and_honors_orientation():
    out = ok(photos.normalize(_jpeg_with_orientation(600, 400)))
    image = Image.open(BytesIO(out))
    assert image.size == (photos.PHOTO_SIZE, photos.PHOTO_SIZE)
    assert dict(image.getexif()) == {}, (
        "EXIF (incl. GPS) must not survive normalization"
    )


def test_normalize_rejects_garbage_and_oversize():
    refused(photos.normalize(b"this is not an image at all"), errors.Invalid)
    refused(photos.normalize(b""), errors.Invalid)
    refused(photos.normalize(b"x" * (photos.MAX_UPLOAD_BYTES + 1)), errors.Invalid)


async def test_set_get_delete_and_versions(database):
    async with db_session() as session:
        v = ok(
            await volunteers.create(session, None, "Pia", "Photo", "pia@example.org")
        )
        volunteer_id = v.id
        stored = ok(
            await photos.set_photo(
                session,
                volunteer_id,
                _png(500, 500),
                uploaded_by=None,
                now=datetime.now(UTC),
            )
        )
        assert len(stored.image) <= photos.PHOTO_MAX_BYTES
        assert stored.content_type == "image/jpeg"
        first_at = stored.uploaded_at

        assert (await photos.get(session, volunteer_id)).image == stored.image
        versions = await photos.versions(session, [volunteer_id])
        assert versions == {volunteer_id: first_at}
        assert await photos.versions(session, []) == {}

        # replace: bytes change and uploaded_at moves (drives the ?v= cache-buster)
        replaced = ok(
            await photos.set_photo(
                session,
                volunteer_id,
                _png(300, 700),
                uploaded_by=None,
                now=datetime.now(UTC),
            )
        )
        assert replaced.uploaded_at >= first_at
        assert (await photos.images(session, [volunteer_id]))[
            volunteer_id
        ] == replaced.image

        ok(await photos.delete_photo(session, volunteer_id))
        assert await photos.get(session, volunteer_id) is None
        ok(await photos.delete_photo(session, volunteer_id))  # idempotent


async def test_unknown_volunteer_raises_lookup_error(database):
    async with db_session() as session:
        refused(
            await photos.set_photo(
                session, 424242, _png(400, 400), uploaded_by=None, now=datetime.now(UTC)
            ),
            errors.NotFound,
        )
        refused(await photos.delete_photo(session, 424242), errors.NotFound)


async def test_deleting_the_volunteer_cascades_the_photo(database):
    async with db_session() as session:
        v = ok(
            await volunteers.create(session, None, "Gone", "Soon", "gone@example.org")
        )
        ok(
            await photos.set_photo(
                session, v.id, _png(400, 400), uploaded_by=None, now=datetime.now(UTC)
            )
        )
        volunteer_id = v.id
    async with db_session() as session:
        ok(await volunteers.delete(session, None, volunteer_id))
        remaining = (
            await session.execute(
                sa.select(VolunteerPhoto).where(
                    VolunteerPhoto.volunteer_id == volunteer_id
                )
            )
        ).scalar_one_or_none()
        assert remaining is None, "ON DELETE CASCADE"


def test_photo_url_changes_when_uploaded_at_moves():
    a = datetime(2026, 7, 31, 12, 0, 0, 100_000, tzinfo=UTC)
    b = datetime(2026, 7, 31, 12, 0, 0, 900_000, tzinfo=UTC)
    assert photos.photo_url(1, a) != photos.photo_url(1, b), (
        "same-second replacements must still bust browser caches"
    )
