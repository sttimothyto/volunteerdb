"""The parish's own logo: normalization and storage.

One image for the whole instance, uploaded by an admin, shown in the app
header, above the login box, and on the public ministries shell. Stored in
Postgres like every other binary here (services/photos.py, TeamPageImage) —
there is no object store, and ui/static is baked into the container image.

normalize() deliberately differs from photos.normalize in three ways, because
a logo is not a headshot:

* **Alpha is preserved, never flattened onto white.** The app header is
  terracotta (#a5573e); a logo composited onto white would render as a white
  box sitting on it.
* **Aspect ratio is preserved** (ImageOps.contain, not ImageOps.fit). A
  wordmark is usually wide, and centre-cropping it to a square would cut the
  parish's name in half.
* **A flat ground is cut away.** Most marks arrive as a JPEG, or a PNG
  exported from one, with no alpha at all: the mark sits on a white card. That
  card is the white box the first rule exists to avoid, so it is made
  transparent — see _cut_flat_ground.
"""

from collections import Counter
from datetime import datetime
from io import BytesIO

import sqlalchemy as sa
from PIL import (
    Image,
    ImageChops,
    ImageFilter,
    ImageMath,
    ImageOps,
    UnidentifiedImageError,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..errors import DomainError, Invalid, invalid, require
from ..fp import Err, Ok, Result
from ..models import SiteLogo
from ..permissions import Actor

# Far more than any placement needs today (the header renders it at 32px, the
# login page at 64px, so a retina screen asks for 128 at most), and that is the
# point: an admin hands us the mark they have, and the parish's own artwork is
# worth keeping at a size a future larger placement — or a printed page — can
# still use. What has to stay bounded is bytes, not pixels: the row is read on
# every uncached /logo request. Hence a ceiling _encode() reaches for by
# dropping colours rather than by refusing the upload.
LOGO_BOX = 1000
LOGO_MAX_BYTES = 500_000
MAX_UPLOAD_BYTES = 10_000_000
CONTENT_TYPE = "image/png"
# The one row. `id` is pinned by a CHECK constraint, so an upload upserts.
ROW_ID = 1

# How far a pixel may sit from the ground colour and still count as ground, as
# the largest of the three channel differences. It only has to clear the card's
# own noise — the ring a JPEG leaves round a hard edge is well above it and is
# matted rather than cut (_coverage) — so it is set low on purpose: a mark this
# close to the card cannot be told from it, and at 32 a cream mark on white
# (a 30-point difference in blue) is eaten whole.
GROUND_TOLERANCE = 24
# ...and the share of the border that must be ground before there is a card to
# cut at all. Below it the upload is a picture that merely has a pale corner,
# and cutting would chew a hole in it.
GROUND_SHARE = 0.9
# How far the ground bleeds into the mark, in pixels: the anti-aliased (or
# JPEG-smeared) rim where a pixel is part ground and part mark. It sets both
# how far past the flat ground the cut reaches and how far a rim pixel looks
# for the mark it belongs to. Odd, because that is the window MaxFilter wants;
# 5 is two pixels each way, which covers both a LANCZOS downscale and the ring
# a JPEG leaves round a hard edge. Wider measures no better and mattes more of
# the mark's own edge than it needs to.
RIM = 5


def normalize(data: bytes) -> Result[bytes, Invalid]:
    """Sync and CPU-bound — call via anyio.to_thread.run_sync from handlers."""
    if len(data) > MAX_UPLOAD_BYTES:
        return invalid("image file larger than 10 MB")
    try:
        img = Image.open(BytesIO(data))
        img = ImageOps.exif_transpose(img)
        # RGBA throughout: a logo with a transparent ground has to keep it
        img = img.convert("RGBA")
        # contain, not fit: shrink to fit the box, never crop, never upscale
        img.thumbnail((LOGO_BOX, LOGO_BOX), Image.Resampling.LANCZOS)
        # after the resize, deliberately: the cut then costs one bounded pass
        # over at most LOGO_BOX², and the rim the resampler blended into the
        # ground is exactly what _cut_flat_ground mattes back to a soft edge
        img = _cut_flat_ground(img)
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        OSError,
        ValueError,
    ):
        return invalid("not a readable image")
    return _encode(img)


def _encode(img: Image.Image) -> Result[bytes, Invalid]:
    """PNG under the byte ceiling, if there is any way to get it there."""
    buffer = BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    encoded = buffer.getvalue()
    if len(encoded) <= LOGO_MAX_BYTES:
        return Ok(encoded)
    # A drawn mark never gets here; a photographed or scanned one does, because
    # its grain is noise no PNG predictor can model, and at LOGO_BOX² there is
    # a lot of it. 256 colours picked from the image (alpha included, which is
    # why FASTOCTREE and not the default median cut) is invisible on flat
    # artwork and merely acceptable on grain — and it is what makes a mark this
    # size affordable to serve.
    buffer = BytesIO()
    img.quantize(colors=256, method=Image.Quantize.FASTOCTREE).save(
        buffer, format="PNG", optimize=True
    )
    encoded = buffer.getvalue()
    if len(encoded) > LOGO_MAX_BYTES:
        return invalid(
            "logo is too detailed to store — try a flatter image, or one with "
            "fewer colours"
        )
    return Ok(encoded)


def _cut_flat_ground(img: Image.Image) -> Image.Image:
    """Make the flat card an opaque mark sits on transparent.

    Only ground *connected to the border* goes, so the white inside a mark —
    lettering on a shield, the counters of a monogram — is left alone. And the
    rim between the two is matted rather than cut square: the mark keeps its
    anti-aliasing, without carrying a pale ghost of the card into it.
    """
    if img.getchannel("A").getextrema()[0] < 255:
        return img  # the uploader already said what is transparent here
    rgb = img.convert("RGB")
    ground = Counter(_frame_pixels(rgb)).most_common(1)[0][0]
    distance = _distance_from(rgb, ground)
    near = distance.point(lambda value: 255 if value <= GROUND_TOLERANCE else 0)
    if _frame_share(near) < GROUND_SHARE:
        return img
    cut = _reachable_from_frame(near).filter(ImageFilter.MaxFilter(RIM))
    alpha = _matte(distance, near, cut)
    if alpha.getbbox() is None:
        return img  # all ground and no mark: not a logo, so not ours to erase
    unmixed = _unmix(rgb, ground, alpha)
    unmixed.putalpha(alpha)
    return unmixed


def _matte(distance: Image.Image, near: Image.Image, cut: Image.Image) -> Image.Image:
    """The alpha channel the cut leaves: clear over the card, opaque over the
    mark, and part-way across the rim between them."""
    clear = Image.new("L", distance.size, 0)
    rim = Image.composite(clear, _coverage(distance), near)
    return Image.composite(rim, Image.new("L", distance.size, 255), cut)


def _coverage(distance: Image.Image) -> Image.Image:
    """How much of a rim pixel the mark actually covers.

    A pixel on the edge of a mark is a mixture of mark and card, and how far it
    has travelled from the card's colour says how much mark is in it — as a
    share of how far the mark itself travelled, which the neighbourhood maximum
    stands in for. Get that share wrong and the cut leaves a halo: too low and
    the mark's edge goes see-through, too high and the card survives as a pale
    outline. The GROUND_TOLERANCE band is not matted at all but cut outright
    (_matte), because down there the distance is the card's own noise rather
    than any amount of mark.
    """
    mark = distance.filter(ImageFilter.MaxFilter(RIM))
    return ImageMath.lambda_eval(
        lambda op: op["convert"](
            op["min"](op["distance"] * 255 / op["max"](op["mark"], 1), 255), "L"
        ),
        distance=distance,
        mark=mark,
    )


def _unmix(
    rgb: Image.Image, ground: tuple[int, int, int], alpha: Image.Image
) -> Image.Image:
    """Take the card back out of the colours it bled into.

    A half-covered rim pixel is half mark and half white card. Drawn over the
    terracotta header at half alpha it would still carry that white — the halo
    again, in the colours this time rather than in the alpha. Solving
    ``C = a·F + (1-a)·G`` for the mark's own F is what removes it. Where alpha
    is 255 — the mark itself, and everything outside the cut — it is the
    identity, and where alpha is 0 the pixel is invisible and the answer does
    not matter.
    """
    return Image.merge(
        "RGB",
        [
            ImageMath.lambda_eval(
                lambda op: op["convert"](
                    op["min"](
                        op["max"](
                            (op["band"] * 255 - op["level"] * (255 - op["alpha"]))
                            / op["max"](op["alpha"], 1),
                            0,
                        ),
                        255,
                    ),
                    "L",
                ),
                band=band,
                level=level,
                alpha=alpha,
            )
            for band, level in zip(rgb.split(), ground)
        ],
    )


def _frame_boxes(size: tuple[int, int]) -> tuple[tuple[int, int, int, int], ...]:
    """The one-pixel frame around the image, as crop boxes. The corners fall in
    two boxes each, which no caller cares about."""
    width, height = size
    return (
        (0, 0, width, 1),
        (0, height - 1, width, height),
        (0, 0, 1, height),
        (width - 1, 0, width, height),
    )


def _frame_pixels(rgb: Image.Image) -> list[tuple[int, int, int]]:
    raw = b"".join(rgb.crop(box).tobytes() for box in _frame_boxes(rgb.size))
    return list(zip(raw[0::3], raw[1::3], raw[2::3]))


def _frame_share(near: Image.Image) -> float:
    """What fraction of the frame is ground — 1.0 for a mark on a clean card."""
    boxes = [near.crop(box) for box in _frame_boxes(near.size)]
    return sum(box.histogram()[255] for box in boxes) / sum(
        box.width * box.height for box in boxes
    )


def _distance_from(rgb: Image.Image, colour: tuple[int, int, int]) -> Image.Image:
    """Per-pixel distance from `colour`: the largest channel difference, which
    beats a mean at telling a tinted mark from its ground."""
    diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, colour))
    red, green, blue = diff.split()
    return ImageChops.lighter(ImageChops.lighter(red, green), blue)


def _reachable_from_frame(near: Image.Image) -> Image.Image:
    """The ground itself: the ground-coloured pixels a walk in from the border
    reaches without crossing the mark.

    A scanline flood fill — fills a whole run at a time and only then looks at
    the rows above and below. Pillow ships ImageDraw.floodfill, but it is a
    Python loop over single pixels and takes seconds at LOGO_BOX²; this runs in
    a fraction of one.
    """
    width, height = near.size
    mask = near.tobytes()
    reached = bytearray(len(mask))
    stack = [(x, y) for y in (0, height - 1) for x in range(width)]
    stack += [(x, y) for x in (0, width - 1) for y in range(height)]
    while stack:
        x, y = stack.pop()
        row = y * width
        if not mask[row + x] or reached[row + x]:
            continue
        left, right = x, x
        while left and mask[row + left - 1] and not reached[row + left - 1]:
            left -= 1
        while (
            right + 1 < width and mask[row + right + 1] and not reached[row + right + 1]
        ):
            right += 1
        reached[row + left : row + right + 1] = b"\xff" * (right - left + 1)
        for neighbour in (y - 1, y + 1):
            if not 0 <= neighbour < height:
                continue
            offset, at = neighbour * width, left
            while at <= right:
                if mask[offset + at] and not reached[offset + at]:
                    # one seed per run: the fill above walks the rest of it
                    stack.append((at, neighbour))
                    while at <= right and mask[offset + at]:
                        at += 1
                at += 1
    return Image.frombytes("L", (width, height), bytes(reached))


async def get(session: AsyncSession) -> SiteLogo | None:
    return await session.get(SiteLogo, ROW_ID)


async def set_logo(
    session: AsyncSession,
    actor: Actor | None,
    image: bytes,
    *,
    now: datetime,
    normalized: bool = False,
) -> Result[None, DomainError]:
    """Replace the site logo. `normalized=True` is the web dialog saying it
    already ran normalize() to build its preview, so the bytes shown are
    exactly the bytes stored."""
    if denied := require(actor is None or actor.is_admin, "set the site logo"):
        return denied
    if normalized:
        stored = image
    else:
        made = normalize(image)
        if isinstance(made, Err):
            return made
        stored = made.value
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
            "uploaded_at": now,
        },
    )
    await session.execute(stmt)
    return Ok(None)


async def delete_logo(
    session: AsyncSession, actor: Actor | None
) -> Result[None, DomainError]:
    """Back to the shipped placeholder (ui/static/logo-placeholder.svg)."""
    if denied := require(actor is None or actor.is_admin, "set the site logo"):
        return denied
    await session.execute(sa.delete(SiteLogo).where(SiteLogo.id == ROW_ID))
    return Ok(None)
