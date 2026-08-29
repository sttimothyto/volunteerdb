"""App-level audit logging: who did what, secrets redacted, rollbacks marked.

Complements test_history.py (the Postgres trigger trail); here we assert on
the structlog events emitted by the audit listeners in volunteerdb/audit.py.
"""

import csv
from io import StringIO

from volunteerdb.db import db_session
from volunteerdb.services import users, volunteers, workload
from volunteerdb.sheets import importer
from volunteerdb.sheets.common import ROSTER_HEADERS

from tests import mint
from tests.fp_helpers import ok


def _by_event(records, event):
    return [r for r in records if r["event"] == event]


def _csv_bytes(header, rows) -> bytes:
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


async def test_insert_logged_with_user_and_commit(database, log_records):
    async with db_session(user_id=42) as session:
        v = ok(await volunteers.create(session, None, "Ada", "Lovelace"))
    inserts = _by_event(log_records, "db.insert")
    assert len(inserts) == 1
    record = inserts[0]
    assert record["level"] == "audit"
    assert record["table"] == "volunteer"
    assert record["pk"] == f"id={v.id}"
    assert record["user"] == "42"
    assert record["values"]["first_name"] == "'Ada'"
    commits = _by_event(log_records, "db.commit")
    assert commits and commits[0]["txn"] == record["txn"]
    assert commits[0]["writes"] == 1


async def test_update_logs_old_to_new_diff(database, log_records):
    async with db_session() as session:
        v = ok(await volunteers.create(session, None, "Ada", "Lovelace"))
    async with db_session(user_id=7) as session:
        ok(await volunteers.update(session, None, v.id, phone="555-0100"))
    updates = _by_event(log_records, "db.update")
    assert len(updates) == 1
    record = updates[0]
    assert record["level"] == "audit"
    assert record["table"] == "volunteer"
    assert record["user"] == "7"
    assert record["changes"]["phone"] == "None → '555-0100'"
    assert "first_name" not in record["changes"]  # unchanged columns stay out


async def test_delete_logs_full_row(database, log_records):
    async with db_session() as session:
        v = ok(await volunteers.create(session, None, "Ada", "Lovelace"))
    async with db_session(user_id=7) as session:
        ok(await volunteers.delete(session, None, v.id))
    deletes = _by_event(log_records, "db.delete")
    assert len(deletes) == 1
    assert deletes[0]["was"]["first_name"] == "'Ada'"
    assert deletes[0]["user"] == "7"


async def test_debug_level_reads_do_not_log_bound_parameters(
    database, log_records, debug_logging
):
    """At DEBUG the read listener also logs the statement's bound parameters.
    authenticate_token binds the stored SHA-256 token digest, so an unredacted
    dump there turns a debug log into a full API-token compromise."""
    async with db_session() as session:
        user, _ = ok(
            await users.create(
                session,
                "dbg@example.org",
                password="test-pass-phrase",
                invite=mint.fresh_invite(),
            )
        )
    async with db_session() as session:
        token = ok(await users.issue_api_token(session, user.id, token=mint.token()))

    async with db_session() as session:
        found = await users.authenticate_token(session, token)
    assert found is not None, "the token must still authenticate"

    reads = _by_event(log_records, "db.read")
    assert any("params" in r for r in reads), (
        "DEBUG must still log parameters, or this test would pass vacuously"
    )

    digest = users._token_digest(token)
    blob = repr(log_records)
    assert digest not in blob, "the stored token digest reached the log verbatim"
    assert token not in blob


async def test_secrets_never_logged(database, log_records):
    async with db_session() as session:
        user, _ = ok(
            await users.create(
                session,
                "sec@example.org",
                password="hunter2-secret-phrase",
                invite=mint.fresh_invite(),
            )
        )
    async with db_session() as session:
        token = ok(await users.issue_api_token(session, user.id, token=mint.token()))
    async with db_session() as session:
        result = ok(
            await users.start_otp_login(
                session, "sec@example.org", now=mint.now(), code=mint.code()
            )
        )
    assert result is not None and result[1] is not None
    code = result[1]

    blob = repr(log_records)
    assert "hunter2-secret-phrase" not in blob
    assert token not in blob
    assert f"'{code}'" not in blob  # the OTP itself (its argon2 hash is redacted)

    app_user_inserts = [
        r for r in _by_event(log_records, "db.insert") if r["table"] == "app_user"
    ]
    assert app_user_inserts[0]["values"]["password_hash"] == "«redacted»"
    token_updates = [
        r
        for r in _by_event(log_records, "db.update")
        if r["table"] == "app_user" and "api_token" in r["changes"]
    ]
    assert token_updates[0]["changes"]["api_token"] == "None → «redacted»"
    otp_updates = [
        r
        for r in _by_event(log_records, "db.update")
        if r["table"] == "app_user" and "otp_hash" in r["changes"]
    ]
    assert otp_updates[0]["changes"]["otp_hash"].endswith("→ «redacted»")


async def test_dry_run_import_rollback_marked(database, log_records, env):
    content = _csv_bytes(
        ROSTER_HEADERS,
        [["", "Cara", "White", "cara@example.org", "555-9", "", "", ""]],
    )
    report = ok(await importer.run_import(env, content, dry_run=True, user_id=None))
    assert not report.applied

    inserts = _by_event(log_records, "db.insert")
    assert inserts, "dry-run still logs the attempted writes"
    rollbacks = _by_event(log_records, "db.rollback")
    assert rollbacks and rollbacks[0]["txn"] == inserts[0]["txn"]
    assert rollbacks[0]["writes_not_applied"] >= 1
    assert not _by_event(log_records, "db.commit")

    summary = _by_event(log_records, "import.finished")
    assert summary[0]["outcome"] == "dry-run (rolled back)"
    assert summary[0]["volunteers_created"] == 1


async def test_api_write_carries_actor_identity(
    database, log_records, client, token_admin
):
    response = await client.post(
        "/api/volunteers",
        json={"first_name": "Log", "last_name": "Test"},
        headers=token_admin,
    )
    assert response.status_code == 201, response.text
    inserts = [
        r for r in _by_event(log_records, "db.insert") if r["table"] == "volunteer"
    ]
    record = inserts[-1]
    assert record["user"].endswith(":admin@example.org")
    assert record["via"] == "api"
    assert record["ip"] == "127.0.0.1"


async def test_reads_logged_at_info(database, log_records, debug_logging):
    """debug_logging because reads are no longer even *computed* at the
    default AUDIT level — _log_execute returns before get_final_froms() for
    a line _ModeFilter would drop anyway."""
    async with db_session() as session:
        await volunteers.search(session)
    reads = _by_event(log_records, "db.read")
    assert any("volunteer" in r["table"] for r in reads)
    assert all(r["level"] == "info" for r in reads)


async def test_reads_skipped_below_info(database, log_records):
    """The default level is AUDIT: no db.read event is built at all."""
    async with db_session() as session:
        await volunteers.search(session)
    assert _by_event(log_records, "db.read") == []


async def test_core_upsert_logged(database, log_records):
    async with db_session(user_id=3) as session:
        ok(
            await workload.set_config(
                session, None, workload.DEFAULT_CONFIG, now=mint.now()
            )
        )
    core_writes = [r for r in _by_event(log_records, "db.insert") if r.get("core")]
    assert core_writes and core_writes[0]["table"] == "app_setting"
    assert core_writes[0]["user"] == "3"


async def test_anonymous_write_shows_dash_user(database, log_records):
    async with db_session() as session:
        ok(await volunteers.create(session, None, "Anon", "Ymous"))
    record = _by_event(log_records, "db.insert")[0]
    assert record["user"] == "-"
    assert record["via"] == "-"
