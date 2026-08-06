"""User/account service: provisioning, API tokens, invites, credential resets."""

import pytest

from volunteerdb.db import db_session
from volunteerdb.services import users, volunteers
from volunteerdb.services.users import _token_digest


async def test_bulk_provision_dedupes_and_skips(database):
    async with db_session() as session:
        family1 = await volunteers.create(
            session, "Ana", "Family", "family@example.org"
        )
        family2 = await volunteers.create(
            session, "Bob", "Family", "family@example.org"
        )
        await volunteers.create(session, "Carl", "Nomail")  # no email: not considered
        inactive = await volunteers.create(session, "Dora", "Gone", "dora@example.org")
        await volunteers.update(session, inactive.id, is_active=False)
        linked = await volunteers.create(session, "Eli", "Linked", "eli@example.org")
        await users.create(session, "eli-account@example.org", volunteer_id=linked.id)

        report = await users.bulk_provision(session)

        assert [v.id for v, _ in report.created] == [family1.id]
        created_user = report.created[0][1]
        assert created_user.email == "family@example.org"
        assert created_user.invite_token is not None, (
            "provisioned accounts are invite-based"
        )
        skipped = {v.id: reason for v, reason in report.skipped}
        assert "already used" in skipped[family2.id]
        assert skipped[linked.id] == "already has an account"
        assert inactive.id not in skipped, "inactive volunteers are ignored entirely"


async def test_bulk_provision_second_run_is_noop(database):
    async with db_session() as session:
        await volunteers.create(session, "Ana", "Solo", "ana@example.org")
        first = await users.bulk_provision(session)
        assert len(first.created) == 1

    async with db_session() as session:
        second = await users.bulk_provision(session)
        assert second.created == []
        assert [reason for _, reason in second.skipped] == ["already has an account"]


async def test_create_links_to_the_volunteer_at_the_same_address(database):
    async with db_session() as session:
        v = await volunteers.create(session, "Bruno", "Cordeiro", "bruno@example.org")

        user = await users.create(session, "  Bruno@Example.ORG ")

        assert user.volunteer_id == v.id, "an account is somebody's login"


async def test_create_declines_ambiguous_or_unavailable_matches(database):
    async with db_session() as session:
        await volunteers.create(session, "Ana", "Family", "family@example.org")
        await volunteers.create(session, "Bob", "Family", "family@example.org")
        taken = await volunteers.create(session, "Cara", "Taken", "cara@example.org")
        await users.create(session, "cara-old@example.org", volunteer_id=taken.id)
        gone = await volunteers.create(session, "Dora", "Gone", "dora@example.org")
        await volunteers.update(session, gone.id, is_active=False)

        family = await users.create(session, "family@example.org")
        second = await users.create(session, "cara@example.org")
        inactive = await users.create(session, "dora@example.org")
        explicit = await users.create(session, "bruno@example.org", volunteer_id=None)

        assert family.volunteer_id is None, "families share an address: no coin flip"
        assert second.volunteer_id is None, "one account per volunteer"
        assert inactive.volunteer_id is None
        assert explicit.volunteer_id is None, "nobody holds that address"


async def test_create_can_opt_out_of_linking(database):
    async with db_session() as session:
        await volunteers.create(session, "Sync", "Bot", "bot@example.org")

        user = await users.create(session, "bot@example.org", link_by_email=False)

        assert user.volunteer_id is None


async def test_bulk_provision_adopts_an_unlinked_account(database):
    """The bcordeiro case: the account was made before the volunteer existed."""
    async with db_session() as session:
        orphan = await users.create(session, "bruno@example.org")
        assert orphan.volunteer_id is None
        v = await volunteers.create(session, "Bruno", "Cordeiro", "bruno@example.org")

        report = await users.bulk_provision(session)

        assert report.created == []
        assert [(vol.id, u.id) for vol, u in report.linked] == [(v.id, orphan.id)]
        assert orphan.volunteer_id == v.id

    async with db_session() as session:
        again = await users.bulk_provision(session)
        assert again.linked == []
        assert [reason for _, reason in again.skipped] == ["already has an account"]


async def test_set_volunteer_relinks_unlinks_and_refuses_a_taken_volunteer(database):
    async with db_session() as session:
        maria = await volunteers.create(session, "Maria", "Alvarez", "m@example.org")
        pedro = await volunteers.create(session, "Pedro", "Sousa", "p@example.org")
        user = await users.create(session, "typo@example.org")
        assert user.volunteer_id is None

        await users.set_volunteer(session, user.id, maria.id)
        assert user.volunteer_id == maria.id

        await users.set_volunteer(session, user.id, None)
        assert user.volunteer_id is None, "an auto-link can be undone"

        await users.set_volunteer(session, user.id, pedro.id)
        rival = await users.create(session, "rival@example.org")
        with pytest.raises(ValueError, match="already linked to typo@example.org"):
            await users.set_volunteer(session, rival.id, pedro.id)

        with pytest.raises(LookupError):
            await users.set_volunteer(session, user.id, 424242)
        with pytest.raises(LookupError):
            await users.set_volunteer(session, 424242, maria.id)


async def test_issue_api_token_revokes_previous(database):
    async with db_session() as session:
        user = await users.create(session, "api@example.org", password="pw-123456")
        first = await users.issue_api_token(session, user.id)
        second = await users.issue_api_token(session, user.id)

        assert await users.authenticate_token(session, first) is None, (
            "old token is revoked"
        )
        assert (await users.authenticate_token(session, second)).id == user.id
        assert user.api_token == _token_digest(second) != second, (
            "only the digest is stored"
        )


async def test_authenticate_token_rejects_inactive_and_empty(database):
    async with db_session() as session:
        user = await users.create(session, "victim@example.org", password="pw-123456")
        token = await users.issue_api_token(session, user.id)
        assert await users.authenticate_token(session, token) is not None

        await users.set_flags(session, user.id, is_active=False)
        assert await users.authenticate_token(session, token) is None
        assert await users.authenticate_token(session, "") is None


async def test_reissue_invite_invalidates_password(database):
    async with db_session() as session:
        user = await users.create(session, "reset@example.org", password="old-pass-1")
        assert (
            await users.authenticate(session, "reset@example.org", "old-pass-1")
            is not None
        )

        invite = await users.reissue_invite(session, user.id)
        assert (
            await users.authenticate(session, "reset@example.org", "old-pass-1") is None
        )

        redeemed = await users.redeem_invite(session, invite, "new-pass-1")
        assert redeemed is not None and redeemed.invite_token is None
        assert await users.redeem_invite(session, invite, "again") is None, "single use"
        assert (
            await users.authenticate(session, "reset@example.org", "new-pass-1")
            is not None
        )


async def test_set_password_clears_invite_and_missing_raises(database):
    async with db_session() as session:
        user = await users.create(session, "invitee@example.org")
        assert user.invite_token is not None and user.password_hash is None

        await users.set_password(session, user.id, "fresh-pass-1")
        assert user.invite_token is None
        assert (
            await users.authenticate(session, "invitee@example.org", "fresh-pass-1")
            is not None
        )

        with pytest.raises(LookupError):
            await users.set_password(session, 424242, "x")
        with pytest.raises(LookupError):
            await users.reissue_invite(session, 424242)
        with pytest.raises(LookupError):
            await users.issue_api_token(session, 424242)
