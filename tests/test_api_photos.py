"""Photo endpoints over HTTP: open-to-any-account by design, size/type limits."""

from io import BytesIO

from PIL import Image

from volunteerdb.db import db_session
from volunteerdb.services import photos, teams, volunteers

from tests.fp_helpers import ok


def _png(width: int = 500, height: int = 400, color=(90, 60, 30)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _upload(content: bytes) -> dict:
    return {"file": ("headshot.png", content, "image/png")}


async def test_any_signed_in_account_may_manage_any_photo(client, seeded, token_member):
    """Pins the product decision: photo upload/view/delete needs only a signed-in
    account, NOT can_edit_volunteer — even for a volunteer on someone else's team."""
    async with db_session() as session:
        other_team = ok(await teams.create(session, None, "Hospitality"))
        zoe = ok(
            await volunteers.create(session, None, "Zoe", "Zimmer", "zoe@example.org")
        )
        from volunteerdb.models import TeamRole
        from volunteerdb.services import memberships

        ok(
            await memberships.assign(
                session, None, zoe.id, other_team.id, TeamRole.member
            )
        )
        zoe_id = zoe.id

    r = await client.put(
        f"/api/volunteers/{zoe_id}/photo", files=_upload(_png()), headers=token_member
    )
    assert r.status_code == 200, r.text
    meta = r.json()
    assert meta["volunteer_id"] == zoe_id
    assert meta["content_type"] == "image/jpeg"
    assert meta["size_bytes"] <= photos.PHOTO_MAX_BYTES

    r = await client.get(f"/api/volunteers/{zoe_id}/photo", headers=token_member)
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    image = Image.open(BytesIO(r.content))
    assert image.size == (photos.PHOTO_SIZE, photos.PHOTO_SIZE)

    r = await client.delete(f"/api/volunteers/{zoe_id}/photo", headers=token_member)
    assert r.status_code == 204
    r = await client.get(f"/api/volunteers/{zoe_id}/photo", headers=token_member)
    assert r.status_code == 404
    r = await client.delete(f"/api/volunteers/{zoe_id}/photo", headers=token_member)
    assert r.status_code == 204, "delete is idempotent"


async def test_replace_bumps_uploaded_at(client, seeded, token_admin):
    volunteer_id = seeded["volunteer_id"]
    r = await client.put(
        f"/api/volunteers/{volunteer_id}/photo",
        files=_upload(_png()),
        headers=token_admin,
    )
    first = r.json()["uploaded_at"]
    r = await client.put(
        f"/api/volunteers/{volunteer_id}/photo",
        files=_upload(_png(color=(10, 10, 200))),
        headers=token_admin,
    )
    assert r.json()["uploaded_at"] >= first


async def test_upload_limits(client, seeded, token_admin):
    volunteer_id = seeded["volunteer_id"]
    r = await client.put(
        f"/api/volunteers/{volunteer_id}/photo",
        files=_upload(b"x" * 10_000_001),
        headers=token_admin,
    )
    assert r.status_code == 413

    r = await client.put(
        f"/api/volunteers/{volunteer_id}/photo",
        files=_upload(b"not an image"),
        headers=token_admin,
    )
    assert r.status_code == 422

    r = await client.put(
        "/api/volunteers/424242/photo", files=_upload(_png()), headers=token_admin
    )
    assert r.status_code == 404


async def test_has_photo_on_list_and_detail(client, seeded, token_admin):
    volunteer_id = seeded["volunteer_id"]
    r = await client.get("/api/volunteers", headers=token_admin)
    (maria,) = [v for v in r.json() if v["id"] == volunteer_id]
    assert maria["has_photo"] is False

    await client.put(
        f"/api/volunteers/{volunteer_id}/photo",
        files=_upload(_png()),
        headers=token_admin,
    )
    r = await client.get("/api/volunteers", headers=token_admin)
    (maria,) = [v for v in r.json() if v["id"] == volunteer_id]
    assert maria["has_photo"] is True
    r = await client.get(f"/api/volunteers/{volunteer_id}", headers=token_admin)
    assert r.json()["has_photo"] is True
