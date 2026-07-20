"""User/account service: provisioning, API tokens, invites, credential resets."""

import pytest

from volunteerdb.db import db_session
from volunteerdb.services import users, volunteers
from volunteerdb.services.users import _token_digest


async def test_bulk_provision_dedupes_and_skips(database):
    async with db_session() as session:
        family1 = await volunteers.create(session, "Ana", "Family", "family@example.org")
        family2 = await volunteers.create(session, "Bob", "Family", "family@example.org")
        await volunteers.create(session, "Carl", "Nomail")  # no email: not considered
        inactive = await volunteers.create(session, "Dora", "Gone", "dora@example.org")
        await volunteers.update(session, inactive.id, is_active=False)
        linked = await volunteers.create(session, "Eli", "Linked", "eli@example.org")
        await users.create(session, "eli-account@example.org", volunteer_id=linked.id)

        report = await users.bulk_provision(session)

        assert [v.id for v, _ in report.created] == [family1.id]
        created_user = report.created[0][1]
        assert created_user.email == "family@example.org"
        assert created_user.invite_token is not None, "provisioned accounts are invite-based"
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


async def test_issue_api_token_revokes_previous(database):
    async with db_session() as session:
        user = await users.create(session, "api@example.org", password="pw-123456")
        first = await users.issue_api_token(session, user.id)
        second = await users.issue_api_token(session, user.id)

        assert await users.authenticate_token(session, first) is None, "old token is revoked"
        assert (await users.authenticate_token(session, second)).id == user.id
        assert user.api_token == _token_digest(second) != second, "only the digest is stored"


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
        assert await users.authenticate(session, "reset@example.org", "old-pass-1") is not None

        invite = await users.reissue_invite(session, user.id)
        assert await users.authenticate(session, "reset@example.org", "old-pass-1") is None

        redeemed = await users.redeem_invite(session, invite, "new-pass-1")
        assert redeemed is not None and redeemed.invite_token is None
        assert await users.redeem_invite(session, invite, "again") is None, "single use"
        assert await users.authenticate(session, "reset@example.org", "new-pass-1") is not None


async def test_set_password_clears_invite_and_missing_raises(database):
    async with db_session() as session:
        user = await users.create(session, "invitee@example.org")
        assert user.invite_token is not None and user.password_hash is None

        await users.set_password(session, user.id, "fresh-pass-1")
        assert user.invite_token is None
        assert await users.authenticate(session, "invitee@example.org", "fresh-pass-1") is not None

        with pytest.raises(LookupError):
            await users.set_password(session, 424242, "x")
        with pytest.raises(LookupError):
            await users.reissue_invite(session, 424242)
        with pytest.raises(LookupError):
            await users.issue_api_token(session, 424242)
