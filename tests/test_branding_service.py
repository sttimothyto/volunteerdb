"""Site logo: normalization invariants, storage semantics, and who may set it.

The two normalization rules here are the ones a headshot deliberately breaks
(services/photos.py flattens onto white and centre-crops square), so they are
asserted rather than assumed: a logo composited onto white renders as a white
box on the terracotta app header, and a centre-cropped wordmark loses the
parish's name.
"""

from io import BytesIO

import pytest
import sqlalchemy as sa
from PIL import Image

from volunteerdb.db import db_session
from volunteerdb.models import SiteLogo
from volunteerdb.permissions import Forbidden, load_actor
from volunteerdb.services import branding, users


def _png(width: int, height: int) -> bytes:
    """A mark on a transparent ground: opaque bar, clear corners."""
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    bar = Image.new("RGBA", (width // 2, height // 2), (176, 125, 43, 255))
    image.paste(bar, (width // 4, height // 4))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


async def _actor(session, email: str, *, admin: bool):
    user, _ = await users.create(session, email, is_admin=admin)
    return await load_actor(session, user)


def test_normalize_keeps_alpha_and_aspect_ratio():
    out = branding.normalize(_png(900, 300))
    image = Image.open(BytesIO(out))
    assert image.format == "PNG"
    assert image.mode == "RGBA", (
        "alpha must survive: the app header is terracotta, and a logo "
        "flattened onto white would render as a white box on it"
    )
    assert image.getpixel((0, 0))[3] == 0, "the transparent ground stayed transparent"
    assert image.size == (branding.LOGO_BOX, 171), (
        "a 3:1 wordmark is scaled to fit the box, never cropped square"
    )


def test_normalize_does_not_upscale_a_small_logo():
    out = branding.normalize(_png(64, 64))
    assert Image.open(BytesIO(out)).size == (64, 64)


def test_normalize_rejects_junk_and_oversized_uploads():
    with pytest.raises(ValueError):
        branding.normalize(b"not an image")
    with pytest.raises(ValueError):
        branding.normalize(b"")
    with pytest.raises(ValueError):
        branding.normalize(b"x" * (branding.MAX_UPLOAD_BYTES + 1))


async def test_set_replaces_rather_than_accumulates(database):
    async with db_session() as session:
        assert await branding.get(session) is None, "no logo until one is uploaded"

        await branding.set_logo(session, None, _png(400, 400))
        first = await branding.get(session)
        assert first.content_type == "image/png"
        assert first.id == branding.ROW_ID
        # snapshot the bytes: `first` is identity-mapped, so re-reading the row
        # hands back this same instance rather than fresh column values
        first_bytes = first.image

        await branding.set_logo(session, None, _png(600, 200))
        # Core select, straight past the identity map, to see what is stored
        stored = (await session.execute(sa.select(SiteLogo.id, SiteLogo.image))).all()
        assert len(stored) == 1, "one logo, replaced in place — not a pile of versions"
        assert stored[0].image != first_bytes, "the upsert replaced the bytes"

        await branding.delete_logo(session, None)
        assert await branding.get(session) is None
        await branding.delete_logo(session, None)  # idempotent


async def test_normalized_bytes_are_stored_verbatim(database):
    """The dialog normalizes to build its preview, so what it showed is what
    gets stored — no second pass that could differ from the preview."""
    async with db_session() as session:
        pre = branding.normalize(_png(500, 500))
        await branding.set_logo(session, None, pre, normalized=True)
        assert (await branding.get(session)).image == pre


async def test_only_admins_may_change_the_logo(database):
    async with db_session() as session:
        member = await _actor(session, "member-logo@example.org", admin=False)
        with pytest.raises(Forbidden):
            await branding.set_logo(session, member, _png(200, 200))
        with pytest.raises(Forbidden):
            await branding.delete_logo(session, member)

        admin = await _actor(session, "admin-logo@example.org", admin=True)
        await branding.set_logo(session, admin, _png(200, 200))
        stored = await branding.get(session)
        assert stored.uploaded_by == admin.user.id, "the upload is attributed"
        await branding.delete_logo(session, admin)
