"""The public "I'm interested" pipeline: service dedup, the per-team Google
application form, the session-free form routes, and — the security-sensitive
part — what each of the two emails may and may not contain.

The applicant-facing mail must stay a fixed template: the form is public and
the recipient address is submitter-chosen, so any echoed text would let a
stranger deliver their words to an arbitrary mailbox through the parish's
sender.
"""

import pytest
import sqlalchemy as sa

from volunteerdb.db import db_session
from volunteerdb.models import Interest, TeamPage, TeamRole
from volunteerdb.services import interest, mail, memberships, pages, teams, volunteers

FORM_URL = "https://docs.google.com/forms/d/e/ABC123/viewform"


@pytest.fixture
def sent_mail(monkeypatch):
    """Captured (to, subject, body) triples; send_email is invoked as a module
    attribute everywhere exactly so this patch works."""
    sent: list[tuple[str, str, str]] = []

    async def fake(to: str, subject: str, text_body: str) -> bool:
        sent.append((to, subject, text_body))
        return True

    monkeypatch.setattr(mail, "send_email", fake)
    return sent


async def _publish(name: str, html: str = "<p>hello</p>") -> int:
    async with db_session() as session:
        team = await teams.create(session, name)
        await pages.set_home_doc_url(
            session, team.id, "https://docs.google.com/document/d/x" + str(team.id)
        )
        session.add(TeamPage(team_id=team.id, html=html, status="ok"))
        return team.id


async def _lead(team_id: int, email: str = "lena@example.org") -> int:
    async with db_session() as session:
        lena = await volunteers.create(session, "Lena", "Leader", email)
        await memberships.assign(session, lena.id, team_id, TeamRole.leader)
        return lena.id


# --- service ---------------------------------------------------------------


async def test_submit_dedups_open_interest_per_team_and_email(database):
    team_id = await _publish("Choir")
    async with db_session() as session:
        first = await interest.submit(
            session, team_id=team_id, name="Ann", email="Ann@Example.ORG"
        )
        assert first is not None and first.email == "ann@example.org", "lowercased"
        assert (
            await interest.submit(
                session, team_id=team_id, name="Ann", email="ann@example.org "
            )
            is None
        ), "an open duplicate is dropped"

        await interest.resolve(session, first.id, resolved_by=None)
        again = await interest.submit(
            session, team_id=team_id, name="Ann", email="ann@example.org"
        )
        assert again is not None, "resolving reopens the door"


async def test_submit_requires_name_and_plausible_email(database):
    team_id = await _publish("Choir")
    async with db_session() as session:
        for name, email in (("", "a@b.org"), ("Ann", ""), ("Ann", "not-an-email")):
            with pytest.raises(ValueError):
                await interest.submit(session, team_id=team_id, name=name, email=email)


async def test_unresolved_and_resolve(database):
    team_id = await _publish("Choir")
    async with db_session() as session:
        row = await interest.submit(
            session, team_id=team_id, name="Ann", email="ann@example.org"
        )
        assert [i.id for i in await interest.unresolved(session, team_id)] == [row.id]
        await interest.resolve(session, row.id, resolved_by=None)
        assert await interest.unresolved(session, team_id) == []
        with pytest.raises(LookupError):
            await interest.resolve(session, 99999, resolved_by=None)


async def test_leader_emails_covers_leader_and_second_only(database):
    team_id = await _publish("Choir")
    async with db_session() as session:
        lena = await volunteers.create(session, "Lena", "Leader", "lena@example.org")
        sam = await volunteers.create(session, "Sam", "Second", "sam@example.org")
        cora = await volunteers.create(session, "Cora", "Core", "cora@example.org")
        noel = await volunteers.create(session, "Noel", "NoEmail")
        await memberships.assign(session, lena.id, team_id, TeamRole.leader)
        await memberships.assign(session, sam.id, team_id, TeamRole.second)
        await memberships.assign(session, cora.id, team_id, TeamRole.core)
        await memberships.assign(session, noel.id, team_id, TeamRole.second)
        assert await interest.leader_emails(session, team_id) == [
            "lena@example.org",
            "sam@example.org",
        ]


async def test_set_application_form_url_accepts_google_forms_only(database):
    async with db_session() as session:
        team = await teams.create(session, "Choir")
        for good in (FORM_URL, "https://forms.gle/AbC123"):
            await teams.set_application_form_url(session, team.id, good)
            assert team.application_form_url == good
        for bad in (
            "https://evil.test/form",
            "https://docs.google.com/document/d/abc",
            "http://docs.google.com/forms/d/abc",  # https only
        ):
            with pytest.raises(ValueError):
                await teams.set_application_form_url(session, team.id, bad)
        await teams.set_application_form_url(session, team.id, None)
        assert team.application_form_url is None
        with pytest.raises(LookupError):
            await teams.set_application_form_url(session, 99999, FORM_URL)


# --- the public routes -----------------------------------------------------


async def test_published_page_carries_the_interest_form(real_app_client):
    await _publish("Choir")
    r = await real_app_client.get("/ministries/choir.html")
    assert r.status_code == 200
    assert 'action="/ministries/choir/interest"' in r.text
    assert 'name="email"' in r.text and 'name="website"' in r.text
    assert "Interested in joining?" in r.text

    r = await real_app_client.get("/ministries/choir.html?sent=1")
    assert "Thank you" in r.text
    assert 'name="email"' not in r.text, "the confirmation replaces the form"


async def _post(client, slug: str, data: dict):
    return await client.post(
        f"/ministries/{slug}/interest", data=data, follow_redirects=False
    )


async def test_interest_post_stores_and_sends_both_emails(real_app_client, sent_mail):
    team_id = await _publish("Choir")
    await _lead(team_id)

    r = await _post(
        real_app_client,
        "choir",
        {
            "name": "Ann Applicant",
            "email": "Ann@Example.org",
            "phone": "555-0100",
            "note": "I sing alto SECRETNOTE",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/ministries/choir.html?sent=1"
    assert r.headers["cache-control"] == "no-store"

    async with db_session() as session:
        (row,) = await interest.unresolved(session, team_id)
        assert row.name == "Ann Applicant" and row.email == "ann@example.org"

    by_recipient = {to: body for to, _, body in sent_mail}
    assert set(by_recipient) == {"lena@example.org", "ann@example.org"}
    assert "SECRETNOTE" in by_recipient["lena@example.org"]
    assert "555-0100" in by_recipient["lena@example.org"]
    applicant_body = by_recipient["ann@example.org"]
    assert "SECRETNOTE" not in applicant_body, "never echo the note to the submitter"
    assert "Ann" not in applicant_body, "no submitted text at all, not even the name"
    assert "follow up" in applicant_body, "no application form linked yet"


async def test_applicant_email_carries_the_application_form_when_set(
    real_app_client, sent_mail
):
    team_id = await _publish("Choir")
    async with db_session() as session:
        await teams.set_application_form_url(session, team_id, FORM_URL)

    await _post(real_app_client, "choir", {"name": "Ann", "email": "ann@example.org"})
    (applicant_body,) = [body for to, _, body in sent_mail if to == "ann@example.org"]
    assert FORM_URL in applicant_body
    assert "follow up" not in applicant_body


async def test_duplicate_open_interest_sends_no_second_mail(real_app_client, sent_mail):
    team_id = await _publish("Choir")
    await _lead(team_id)
    data = {"name": "Ann", "email": "ann@example.org"}

    r = await _post(real_app_client, "choir", data)
    assert r.status_code == 303
    first_count = len(sent_mail)

    r = await _post(real_app_client, "choir", data)
    assert r.status_code == 303, "same thank-you either way — no oracle"
    assert len(sent_mail) == first_count, "a repeat POST mails nobody again"
    async with db_session() as session:
        assert len(await interest.unresolved(session, team_id)) == 1


async def test_honeypot_pretends_success_and_stores_nothing(real_app_client, sent_mail):
    team_id = await _publish("Choir")
    r = await _post(
        real_app_client,
        "choir",
        {"name": "Bot", "email": "bot@example.org", "website": "https://spam"},
    )
    assert r.status_code == 303
    assert sent_mail == []
    async with db_session() as session:
        assert await interest.unresolved(session, team_id) == []


async def test_per_ip_throttle_caps_submissions(real_app_client, sent_mail):
    team_id = await _publish("Choir")
    for i in range(5):
        r = await _post(
            real_app_client,
            "choir",
            {"name": f"P{i}", "email": f"p{i}@example.org"},
        )
        assert r.status_code == 303
    r = await _post(real_app_client, "choir", {"name": "P6", "email": "p6@example.org"})
    assert r.status_code == 303, "throttled looks exactly like accepted"
    async with db_session() as session:
        rows = await interest.unresolved(session, team_id)
        assert len(rows) == 5 and "p6@example.org" not in {r.email for r in rows}


async def test_per_email_throttle_caps_mail_to_one_address(real_app_client, sent_mail):
    """Resolving reopens the dedup, so the per-address throttle is what stops
    a stranger's mailbox being pelted within one day."""
    team_id = await _publish("Choir")
    for _ in range(3):
        await _post(
            real_app_client, "choir", {"name": "Ann", "email": "ann@example.org"}
        )
        async with db_session() as session:
            for row in await interest.unresolved(session, team_id):
                await interest.resolve(session, row.id, resolved_by=None)
    before = len(sent_mail)
    await _post(real_app_client, "choir", {"name": "Ann", "email": "ann@example.org"})
    assert len(sent_mail) == before, "4th submission for one address in a day"
    async with db_session() as session:
        assert await interest.unresolved(session, team_id) == []


async def test_interest_post_to_unpublished_slug_404s(real_app_client, sent_mail):
    async with db_session() as session:
        await teams.create(session, "Quiet Team")
    r = await _post(
        real_app_client, "quiet-team", {"name": "Ann", "email": "a@example.org"}
    )
    assert r.status_code == 404
    assert sent_mail == []
    async with db_session() as session:
        assert (await session.execute(sa.select(Interest))).all() == []


async def test_interest_rows_cascade_with_their_team(database):
    team_id = await _publish("Ephemeral")
    async with db_session() as session:
        await interest.submit(
            session, team_id=team_id, name="Ann", email="ann@example.org"
        )
    async with db_session() as session:
        await teams.delete(session, team_id)
    async with db_session() as session:
        assert (await session.execute(sa.select(Interest))).all() == []
