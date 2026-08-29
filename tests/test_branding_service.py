"""Site logo: normalization invariants, storage semantics, and who may set it.

The normalization rules here are the ones a headshot deliberately breaks
(services/photos.py flattens onto white and centre-crops square), so they are
asserted rather than assumed: a logo composited onto white renders as a white
box on the terracotta app header, and a centre-cropped wordmark loses the
parish's name. The third rule — cutting the white card a JPEG mark arrives on
— is that same white box coming in by the back door, so it is asserted the
same way, along with what the cut must never do: eat the white *inside* a
mark, thin a mark barely paler than its card, touch a picture that never had
a card, or leave a bright rim tracing the logo on the header.
"""

from datetime import UTC, datetime
from io import BytesIO

import sqlalchemy as sa
from PIL import Image, ImageDraw, ImageFilter

from volunteerdb import errors
from volunteerdb.actors import load_actor
from volunteerdb.db import db_session
from volunteerdb.models import SiteLogo
from volunteerdb.services import branding, users

from tests import mint
from tests.fp_helpers import ok, refused

# ui/static/theme.css --vdb-header-bg: what the cut has to look right against
HEADER = (165, 87, 62)


def _png(width: int, height: int) -> bytes:
    """A mark on a transparent ground: opaque bar, clear corners."""
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    bar = Image.new("RGBA", (width // 2, height // 2), (176, 125, 43, 255))
    image.paste(bar, (width // 4, height // 4))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _luminance(colour: tuple[int, int, int]) -> int:
    return Image.new("RGB", (1, 1), colour).convert("L").getpixel((0, 0))


def _encoded(image: Image.Image, **save: object) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format=save.pop("format", "PNG"), **save)
    return buffer.getvalue()


def _card(size: int = 600) -> Image.Image:
    """The usual upload: a terracotta disc, white lettering *inside* it, the
    whole thing sitting on a white card. No alpha anywhere."""
    image = Image.new("RGB", (size, size), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.ellipse(
        (size // 6, size // 6, 5 * size // 6, 5 * size // 6), fill=(165, 87, 62)
    )
    draw.rectangle(
        (size // 2 - 30, size // 2 - 30, size // 2 + 30, size // 2 + 30),
        fill=(255, 255, 255),
    )
    return image


def _opened(data: bytes) -> Image.Image:
    """Stored bytes as RGBA — .convert() because a heavily detailed logo is
    stored from a palette (branding._encode), where alpha lives in the tRNS
    chunk rather than in a band of its own."""
    return Image.open(BytesIO(data)).convert("RGBA")


async def _actor(session, email: str, *, admin: bool):
    user, _ = ok(
        await users.create(session, email, is_admin=admin, invite=mint.fresh_invite())
    )
    return await load_actor(session, user)


def test_normalize_keeps_alpha_and_aspect_ratio():
    out = ok(branding.normalize(_png(1800, 600)))
    image = Image.open(BytesIO(out))
    assert image.format == "PNG"
    assert image.mode == "RGBA", (
        "alpha must survive: the app header is terracotta, and a logo "
        "flattened onto white would render as a white box on it"
    )
    assert image.getpixel((0, 0))[3] == 0, "the transparent ground stayed transparent"
    assert image.size == (branding.LOGO_BOX, branding.LOGO_BOX // 3), (
        "a 3:1 wordmark is scaled to fit the box, never cropped square"
    )


def test_normalize_does_not_upscale_a_small_logo():
    out = ok(branding.normalize(_png(64, 64)))
    assert Image.open(BytesIO(out)).size == (64, 64)


def test_normalize_cuts_the_white_card_a_jpeg_mark_arrives_on():
    """The common upload — a mark exported to JPEG — has no alpha at all, so
    without this it is the white box the alpha rule exists to prevent."""
    image = _opened(
        ok(branding.normalize(_encoded(_card(), format="JPEG", quality=90)))
    )
    width, height = image.size
    assert image.getpixel((0, 0))[3] == 0, "the corner of the card is gone"
    assert image.getpixel((width // 2, 2))[3] == 0, "so is the card above the disc"
    assert image.getpixel((width // 2, height // 2 - height // 4))[3] == 255, (
        "the disc itself is opaque"
    )
    assert image.getpixel((width // 2, height // 2)) == (255, 255, 255, 255), (
        "white *inside* the mark is the mark: only ground reachable from the "
        "border is cut, or lettering on a shield would be punched out"
    )


def test_normalize_leaves_no_pale_halo_where_the_card_was_cut():
    """The rim of a mark is part mark and part card. Cut it square, or matte it
    with the card's colour still mixed in, and what is left is a bright outline
    tracing the logo on the terracotta header — the white box again, in
    miniature. Nothing composited over the header may come out lighter than the
    header itself."""
    card = Image.new("RGB", (800, 800), (255, 255, 255))
    ImageDraw.Draw(card).ellipse((150, 150, 650, 650), fill=(20, 20, 20))
    stored = _opened(ok(branding.normalize(_encoded(card, format="JPEG", quality=90))))
    header = Image.new("RGB", stored.size, HEADER)
    header.paste(stored, (0, 0), stored)
    lightest = header.convert("L").getextrema()[1]
    assert lightest <= _luminance(HEADER) + 2, (
        "a dark mark on terracotta cannot be brighter than the terracotta"
    )


def test_normalize_leaves_a_picture_without_a_card_alone():
    """Grain to the edges is a photograph, not a mark on a card. Cutting the
    pixels that happen to sit near its modal border colour would chew holes."""
    noise = Image.effect_noise((400, 400), 60).convert("RGB")
    stored = _opened(ok(branding.normalize(_encoded(noise))))
    assert stored.getchannel("A").getextrema() == (255, 255), "nothing was cut"


def test_normalize_keeps_a_mark_barely_paler_than_its_card():
    """GROUND_TOLERANCE is the card's own noise floor, not a judgement about
    what looks like background: cream on white is a 30-point difference in one
    channel, and a tolerance generous enough to swallow that eats the logo."""
    card = Image.new("RGB", (600, 600), (255, 255, 255))
    ImageDraw.Draw(card).ellipse((100, 100, 500, 500), fill=(246, 240, 225))
    stored = _opened(ok(branding.normalize(_encoded(card, format="JPEG", quality=90))))
    assert stored.getpixel((300, 300))[3] == 255, "the pale mark survived"
    assert stored.getpixel((5, 5))[3] == 0, "the card around it did not"


def test_normalize_does_not_erase_an_upload_that_is_all_card():
    """Nothing but ground is not a mark on a ground. Cutting it would store a
    logo that is entirely invisible, which is worse than storing what came."""
    blank = Image.new("RGB", (300, 300), (255, 255, 255))
    stored = _opened(ok(branding.normalize(_encoded(blank))))
    assert stored.getchannel("A").getextrema() == (255, 255)


def test_normalize_leaves_an_upload_that_already_has_alpha_alone():
    """A designer's PNG already says what is transparent; a white card drawn
    on purpose inside it is a decision, not an accident to be undone."""
    logo = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
    logo.paste(Image.new("RGBA", (200, 200), (255, 255, 255, 255)), (100, 100))
    stored = _opened(ok(branding.normalize(_encoded(logo))))
    assert stored.getpixel((200, 200)) == (255, 255, 255, 255), "the card is kept"
    assert stored.getpixel((5, 5))[3] == 0, "and the ground stays clear"


def test_normalize_drops_colours_rather_than_refusing_a_detailed_logo():
    """A scanned mark carries grain no PNG predictor can model, and at
    LOGO_BOX² there is enough of it to blow the byte ceiling that keeps /logo
    cheap. Palette it instead of rejecting the upload."""
    grain = Image.effect_noise((1000, 1000), 30).convert("RGB")
    grain = Image.blend(grain, Image.new("RGB", grain.size, (255, 255, 255)), 0.2)
    grain = grain.filter(ImageFilter.GaussianBlur(0.4))
    assert len(_encoded(grain)) > branding.LOGO_MAX_BYTES, "the fixture is detailed"
    out = ok(branding.normalize(_encoded(grain)))
    assert len(out) <= branding.LOGO_MAX_BYTES
    assert Image.open(BytesIO(out)).size == (branding.LOGO_BOX, branding.LOGO_BOX)


def test_normalize_rejects_junk_and_oversized_uploads():
    refused(branding.normalize(b"not an image"), errors.Invalid)
    refused(branding.normalize(b""), errors.Invalid)
    refused(branding.normalize(b"x" * (branding.MAX_UPLOAD_BYTES + 1)), errors.Invalid)


async def test_set_replaces_rather_than_accumulates(database):
    async with db_session() as session:
        assert await branding.get(session) is None, "no logo until one is uploaded"

        ok(
            await branding.set_logo(
                session, None, _png(400, 400), now=datetime.now(UTC)
            )
        )
        first = await branding.get(session)
        assert first.content_type == "image/png"
        assert first.id == branding.ROW_ID
        # snapshot the bytes: `first` is identity-mapped, so re-reading the row
        # hands back this same instance rather than fresh column values
        first_bytes = first.image

        ok(
            await branding.set_logo(
                session, None, _png(600, 200), now=datetime.now(UTC)
            )
        )
        # Core select, straight past the identity map, to see what is stored
        stored = (await session.execute(sa.select(SiteLogo.id, SiteLogo.image))).all()
        assert len(stored) == 1, "one logo, replaced in place — not a pile of versions"
        assert stored[0].image != first_bytes, "the upsert replaced the bytes"

        ok(await branding.delete_logo(session, None))
        assert await branding.get(session) is None
        ok(await branding.delete_logo(session, None))  # idempotent


async def test_normalized_bytes_are_stored_verbatim(database):
    """The dialog normalizes to build its preview, so what it showed is what
    gets stored — no second pass that could differ from the preview."""
    async with db_session() as session:
        pre = ok(branding.normalize(_png(500, 500)))
        ok(
            await branding.set_logo(
                session, None, pre, normalized=True, now=datetime.now(UTC)
            )
        )
        assert (await branding.get(session)).image == pre


async def test_only_admins_may_change_the_logo(database):
    async with db_session() as session:
        member = await _actor(session, "member-logo@example.org", admin=False)
        refused(
            await branding.set_logo(
                session, member, _png(200, 200), now=datetime.now(UTC)
            ),
            errors.Forbidden,
        )
        refused(await branding.delete_logo(session, member), errors.Forbidden)

        admin = await _actor(session, "admin-logo@example.org", admin=True)
        ok(
            await branding.set_logo(
                session, admin, _png(200, 200), now=datetime.now(UTC)
            )
        )
        stored = await branding.get(session)
        assert stored.uploaded_by == admin.user.id, "the upload is attributed"
        ok(await branding.delete_logo(session, admin))
