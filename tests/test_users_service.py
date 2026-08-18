"""User/account service: provisioning, API tokens, invites, credential resets."""

from datetime import UTC, datetime, timedelta

import pytest

from volunteerdb.db import db_session
from volunteerdb.passwords import WeakPassword
from volunteerdb.services import users, volunteers
from volunteerdb.services.users import _token_digest


async def test_bulk_provision_dedupes_and_skips(database):
    async with db_session() as session:
        family1 = await volunteers.create(
            session, None, "Ana", "Family", "family@example.org"
        )
        family2 = await volunteers.create(
            session, None, "Bob", "Family", "family@example.org"
        )
        await volunteers.create(
            session, None, "Carl", "Nomail"
        )  # no email: not considered
        inactive = await volunteers.create(
            session, None, "Dora", "Gone", "dora@example.org"
        )
        await volunteers.update(session, None, inactive.id, is_active=False)
        linked = await volunteers.create(
            session, None, "Eli", "Linked", "eli@example.org"
        )
        await users.create(session, "eli-account@example.org", volunteer_id=linked.id)

        report = await users.bulk_provision(session, None)

        assert [v.id for v, _, _ in report.created] == [family1.id]
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
        await volunteers.create(session, None, "Ana", "Solo", "ana@example.org")
        first = await users.bulk_provision(session, None)
        assert len(first.created) == 1

    async with db_session() as session:
        second = await users.bulk_provision(session, None)
        assert second.created == []
        assert [reason for _, reason in second.skipped] == ["already has an account"]


async def test_create_links_to_the_volunteer_at_the_same_address(database):
    async with db_session() as session:
        v = await volunteers.create(
            session, None, "Bruno", "Cordeiro", "bruno@example.org"
        )

        user, _ = await users.create(session, "  Bruno@Example.ORG ")

        assert user.volunteer_id == v.id, "an account is somebody's login"


async def test_create_declines_ambiguous_or_unavailable_matches(database):
    async with db_session() as session:
        await volunteers.create(session, None, "Ana", "Family", "family@example.org")
        await volunteers.create(session, None, "Bob", "Family", "family@example.org")
        taken = await volunteers.create(
            session, None, "Cara", "Taken", "cara@example.org"
        )
        await users.create(session, "cara-old@example.org", volunteer_id=taken.id)
        gone = await volunteers.create(
            session, None, "Dora", "Gone", "dora@example.org"
        )
        await volunteers.update(session, None, gone.id, is_active=False)

        family, _ = await users.create(session, "family@example.org")
        second, _ = await users.create(session, "cara@example.org")
        inactive, _ = await users.create(session, "dora@example.org")
        explicit, _ = await users.create(
            session, "bruno@example.org", volunteer_id=None
        )

        assert family.volunteer_id is None, "families share an address: no coin flip"
        assert second.volunteer_id is None, "one account per volunteer"
        assert inactive.volunteer_id is None
        assert explicit.volunteer_id is None, "nobody holds that address"


async def test_create_can_opt_out_of_linking(database):
    async with db_session() as session:
        await volunteers.create(session, None, "Sync", "Bot", "bot@example.org")

        user, _ = await users.create(session, "bot@example.org", link_by_email=False)

        assert user.volunteer_id is None


async def test_bulk_provision_adopts_an_unlinked_account(database):
    """The bcordeiro case: the account was made before the volunteer existed."""
    async with db_session() as session:
        orphan, _ = await users.create(session, "bruno@example.org")
        assert orphan.volunteer_id is None
        v = await volunteers.create(
            session, None, "Bruno", "Cordeiro", "bruno@example.org"
        )

        report = await users.bulk_provision(session, None)

        assert report.created == []
        assert [(vol.id, u.id) for vol, u in report.linked] == [(v.id, orphan.id)]
        assert orphan.volunteer_id == v.id

    async with db_session() as session:
        again = await users.bulk_provision(session, None)
        assert again.linked == []
        assert [reason for _, reason in again.skipped] == ["already has an account"]


async def test_set_volunteer_relinks_unlinks_and_refuses_a_taken_volunteer(database):
    async with db_session() as session:
        maria = await volunteers.create(
            session, None, "Maria", "Alvarez", "m@example.org"
        )
        pedro = await volunteers.create(
            session, None, "Pedro", "Sousa", "p@example.org"
        )
        user, _ = await users.create(session, "typo@example.org")
        assert user.volunteer_id is None

        await users.set_volunteer(session, user.id, maria.id)
        assert user.volunteer_id == maria.id

        await users.set_volunteer(session, user.id, None)
        assert user.volunteer_id is None, "an auto-link can be undone"

        await users.set_volunteer(session, user.id, pedro.id)
        rival, _ = await users.create(session, "rival@example.org")
        with pytest.raises(ValueError, match="already linked to typo@example.org"):
            await users.set_volunteer(session, rival.id, pedro.id)

        with pytest.raises(LookupError):
            await users.set_volunteer(session, user.id, 424242)
        with pytest.raises(LookupError):
            await users.set_volunteer(session, 424242, maria.id)


async def test_issue_api_token_revokes_previous(database):
    async with db_session() as session:
        user, _ = await users.create(
            session, "api@example.org", password="api-pass-phrase-1"
        )
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
        user, _ = await users.create(
            session, "victim@example.org", password="api-pass-phrase-1"
        )
        token = await users.issue_api_token(session, user.id)
        assert await users.authenticate_token(session, token) is not None

        await users.set_flags(session, user.id, is_active=False)
        assert await users.authenticate_token(session, token) is None
        assert await users.authenticate_token(session, "") is None


async def test_reissue_invite_invalidates_password(database):
    async with db_session() as session:
        user, _ = await users.create(
            session, "reset@example.org", password="old-pass-phrase-1"
        )
        assert (
            await users.authenticate(session, "reset@example.org", "old-pass-phrase-1")
            is not None
        )

        invite = await users.reissue_invite(session, user.id)
        assert (
            await users.authenticate(session, "reset@example.org", "old-pass-phrase-1")
            is None
        )

        redeemed = await users.redeem_invite(
            session, invite, "new-pass-phrase-1", agreed_to_confidentiality=True
        )
        assert redeemed is not None and redeemed.invite_token is None
        assert (
            await users.redeem_invite(
                session, invite, "again", agreed_to_confidentiality=True
            )
            is None
        ), "single use"
        assert (
            await users.authenticate(session, "reset@example.org", "new-pass-phrase-1")
            is not None
        )


async def test_set_password_clears_invite_and_missing_raises(database):
    async with db_session() as session:
        user, _ = await users.create(session, "invitee@example.org")
        assert user.invite_token is not None and user.password_hash is None

        await users.set_password(session, user.id, "fresh-pass-phrase-1")
        assert user.invite_token is None
        assert (
            await users.authenticate(
                session, "invitee@example.org", "fresh-pass-phrase-1"
            )
            is not None
        )

        with pytest.raises(LookupError):
            await users.set_password(session, 424242, "x")
        with pytest.raises(LookupError):
            await users.reissue_invite(session, 424242)
        with pytest.raises(LookupError):
            await users.issue_api_token(session, 424242)


async def test_invite_links_expire(database):
    """The invite link is also the reset link, so it is a recovery credential
    sitting in a mailbox: NIST SP 800-63B §4.2.1.2 caps one emailed to an
    address at 24 hours."""
    async with db_session() as session:
        # the plaintext comes back from create(); only its digest is stored
        user, token = await users.create(session, "slow@example.org")
        assert user.invite_expires_at is not None
        assert users.invite_live(user)

        user.invite_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.flush()
        assert not users.invite_live(user)
        assert (
            await users.redeem_invite(
                session, token, None, agreed_to_confidentiality=True
            )
            is None
        ), "an expired link is refused exactly like an unknown one"
        assert user.invite_token is not None, "and is not silently consumed"

        # the account is not stranded: an emailed code still signs it in
        _, code = await users.start_otp_login(session, "slow@example.org")
        assert await users.verify_otp(session, "slow@example.org", code) is not None
        assert user.invite_token is None and user.invite_expires_at is None


async def test_invite_volunteer_creates_a_linked_passwordless_account(database):
    async with db_session() as session:
        nils = await volunteers.create(
            session, None, "Nils", "Nobody", "Nils@Example.org"
        )

        account, token = await users.invite_volunteer(session, nils.id)

        assert account.volunteer_id == nils.id, "linked, or they sign in to nothing"
        assert account.email == "nils@example.org", "normalized"
        assert account.password_hash is None, "they choose their own, or none"
        assert not account.is_admin, "a leader can never mint an admin"
        assert token and users.invite_live(account)
        assert await users.account_for_volunteer(session, nils.id) is account


async def test_invite_volunteer_rearms_a_link_nobody_used(database):
    """The whole safety argument for handing this to a ministry leader: an
    account with no password that has never been signed into holds no
    credential, so re-arming it destroys nothing."""
    async with db_session() as session:
        nils = await volunteers.create(
            session, None, "Nils", "Nobody", "nils@example.org"
        )
        account, first = await users.invite_volunteer(session, nils.id)

        # let it lapse unredeemed, as it does after a week of nobody reading email
        account.invite_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.flush()
        assert not users.invite_live(account)

        again, second = await users.invite_volunteer(session, nils.id)
        assert again is account, "the same account, not a second one"
        assert second != first, "a genuinely new token"
        assert users.invite_live(account)


async def test_invite_volunteer_never_touches_a_usable_credential(database):
    """reissue_invite clears the password on purpose — that is the admin's
    hammer for a compromised account. A leader must not be able to swing it."""
    async with db_session() as session:
        settled = await volunteers.create(
            session, None, "Opal", "Online", "opal@example.org"
        )
        account, _ = await users.create(
            session,
            "opal@example.org",
            volunteer_id=settled.id,
            password="settled-pass-phrase-1",
        )
        held = account.password_hash

        with pytest.raises(ValueError, match="already has a working account"):
            await users.invite_volunteer(session, settled.id)
        assert account.password_hash == held, "the password survived"

        # same refusal for a passwordless account that has been signed into
        otp_only = await volunteers.create(
            session, None, "Iris", "Code", "iris@example.org"
        )
        used, _ = await users.create(
            session, "iris@example.org", volunteer_id=otp_only.id
        )
        used.password_hash = None
        used.last_login_at = datetime.now(UTC)
        await session.flush()
        with pytest.raises(ValueError, match="already has a working account"):
            await users.invite_volunteer(session, otp_only.id)


async def test_invite_volunteer_refuses_what_only_an_admin_can_fix(database):
    async with db_session() as session:
        # archived volunteer
        gone = await volunteers.create(
            session, None, "Dora", "Gone", "dora@example.org"
        )
        await volunteers.update(session, None, gone.id, is_active=False)
        with pytest.raises(ValueError, match="archived"):
            await users.invite_volunteer(session, gone.id)

        # no address to send anything to
        quiet = await volunteers.create(session, None, "Hank", "Host")
        with pytest.raises(ValueError, match="no email address"):
            await users.invite_volunteer(session, quiet.id)

        # switched-off account: an admin turned it off deliberately
        off = await volunteers.create(
            session, None, "Quin", "Quiet", "quin@example.org"
        )
        account, _ = await users.create(
            session, "quin@example.org", volunteer_id=off.id
        )
        account.is_active = False
        await session.flush()
        with pytest.raises(ValueError, match="switched off"):
            await users.invite_volunteer(session, off.id)

        with pytest.raises(LookupError):
            await users.invite_volunteer(session, 424242)


async def test_invite_volunteer_will_not_adopt_a_stranger_at_the_same_address(database):
    """bulk_provision adopts an unlinked account at the same address; this
    deliberately does not. volunteer.email is not unique — families share one —
    so adopting would hand a parent's login to their child, and that is an
    admin's judgement call, not a side effect of a leader's button."""
    async with db_session() as session:
        parent = await volunteers.create(
            session, None, "Ana", "Family", "family@example.org"
        )
        child = await volunteers.create(
            session, None, "Bob", "Family", "family@example.org"
        )
        theirs, _ = await users.invite_volunteer(session, parent.id)
        before = theirs.volunteer_id

        with pytest.raises(ValueError, match="already signs in"):
            await users.invite_volunteer(session, child.id)
        assert theirs.volunteer_id == before, "the sibling's account is untouched"
        assert await users.account_for_volunteer(session, child.id) is None


async def test_invitable_agrees_with_what_invite_volunteer_does(database):
    """The badge decides whether to offer the control; the service decides
    whether to honour it. If they ever disagree, a leader is shown a button that
    refuses them."""
    from volunteerdb.ui.account_status import invitable

    async with db_session() as session:
        cases: list[tuple[str, int]] = []

        fresh = await volunteers.create(
            session, None, "New", "Person", "new@example.org"
        )
        cases.append(("no account", fresh.id))

        lapsed_v = await volunteers.create(
            session, None, "Lap", "Sed", "lap@example.org"
        )
        lapsed_a, _ = await users.invite_volunteer(session, lapsed_v.id)
        lapsed_a.invite_expires_at = datetime.now(UTC) - timedelta(seconds=1)

        settled_v = await volunteers.create(
            session, None, "Set", "Tled", "set@example.org"
        )
        await users.create(
            session,
            "set@example.org",
            volunteer_id=settled_v.id,
            password="settled-pass-phrase-1",
        )
        cases.append(("has a password", settled_v.id))

        off_v = await volunteers.create(session, None, "Off", "Line", "off@example.org")
        off_a, _ = await users.create(session, "off@example.org", volunteer_id=off_v.id)
        off_a.is_active = False
        cases.append(("switched off", off_v.id))
        await session.flush()

        cases.append(("lapsed, never used", lapsed_v.id))

        for label, vid in cases:
            account = await users.account_for_volunteer(session, vid)
            offered = invitable(account)
            try:
                await users.invite_volunteer(session, vid)
                honoured = True
            except ValueError:
                honoured = False
            assert offered == honoured, (
                f"{label}: badge offers={offered} but service honours={honoured}"
            )


async def test_reissue_invite_arms_a_fresh_window(database):
    async with db_session() as session:
        user, _ = await users.create(
            session, "lost@example.org", password="old-pass-phrase-1"
        )
        assert user.invite_token is None and user.invite_expires_at is None

        await users.reissue_invite(session, user.id)
        assert users.invite_live(user)
        expected = datetime.now(UTC) + users.invite_ttl()
        assert abs((user.invite_expires_at - expected).total_seconds()) < 60


async def test_weak_passwords_are_refused_on_every_path(database):
    """passwords.check is enforced at the service layer, so no caller — GUI,
    API, seed script or deploy bootstrap — can leave a weak one behind."""
    async with db_session() as session:
        with pytest.raises(WeakPassword, match="too short"):
            await users.create(session, "weak@example.org", password="short")
        assert await users.get_by_email(session, "weak@example.org") is None

        user, _ = await users.create(
            session, "coordinator@example.org", password="cedar lamp figs"
        )
        with pytest.raises(WeakPassword, match="well-known"):
            await users.set_password(session, user.id, "passwordpassword")
        with pytest.raises(WeakPassword, match="email address or the name"):
            await users.set_password(session, user.id, "coordinator-2026")

        invited, invited_token = await users.create(session, "invited@example.org")
        with pytest.raises(WeakPassword, match="too short"):
            await users.redeem_invite(
                session, invited_token, "short", agreed_to_confidentiality=True
            )
        assert invited.invite_token is not None, "a refused password spends nothing"


async def test_redeem_invite_requires_confidentiality_agreement(database):
    """The service is the choke point: no caller can complete signup without
    accepting the confidentiality notice, and the acceptance moment lands on
    the account."""
    async with db_session() as session:
        user, token = await users.create(session, "agrees@example.org")
        with pytest.raises(ValueError, match="confidentiality"):
            await users.redeem_invite(
                session, token, None, agreed_to_confidentiality=False
            )
        assert user.invite_token is not None, "a refused redemption spends nothing"
        assert user.confidentiality_agreed_at is None

        redeemed = await users.redeem_invite(
            session, token, None, agreed_to_confidentiality=True
        )
        assert redeemed is not None
        assert redeemed.confidentiality_agreed_at is not None


async def test_clear_password_drops_api_access_too(database):
    async with db_session() as session:
        user, _ = await users.create(
            session, "quits@example.org", password="cedar lamp figs"
        )
        token = await users.issue_api_token(session, user.id)

        await users.clear_password(session, user.id)
        assert user.password_hash is None
        assert await users.authenticate_token(session, token) is None, (
            "tokens are issued against a password; removing it revokes them"
        )
        assert (
            await users.authenticate(session, "quits@example.org", "cedar lamp figs")
            is None
        )


async def test_accounts_by_volunteer_maps_only_the_linked_ones(database):
    async with db_session() as session:
        linked = await volunteers.create(session, None, "Lin", "Ked", "lin@example.org")
        bare = await volunteers.create(
            session, None, "Bare", "Foot", "bare@example.org"
        )
        account, _ = await users.create(
            session, "lin@example.org", volunteer_id=linked.id
        )
        # an account belonging to nobody must not land in the map
        await users.create(session, "bot@example.org", link_by_email=False)

        by_volunteer = await users.accounts_by_volunteer(session, [linked.id, bare.id])
        assert by_volunteer == {linked.id: account}
        assert await users.accounts_by_volunteer(session, []) == {}

        assert await users.account_for_volunteer(session, linked.id) is account
        assert await users.account_for_volunteer(session, bare.id) is None
