"""The effect interpreter: the one place mail is sent, audit lines written
and throttle charges taken, after a commit. Its contract is that it never
raises and reports honestly -- a toast or a 200 for work that did commit
must not become an error for work that merely failed to be announced."""

from datetime import UTC, datetime

from volunteerdb import effects
from volunteerdb.effects import Audit, EffectReport, SendMail, ThrottleHit, delivered

from tests.fakes import FailingMailer, FakeClock, RecordingMailer

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
MAIL = SendMail("maria@example.org", "Hello", "body")


async def test_every_effect_is_performed_in_order(env, env_sent, log_records):
    clock = FakeClock(T0)
    report = await effects.run(
        [
            Audit("test.audit", (("who", "maria"),)),
            MAIL,
            ThrottleHit("pw:maria@example.org"),
            SendMail("lena@example.org", "Second", "body"),
        ],
        env.with_(clock=clock),
    )
    assert report == EffectReport(mailed=2, failed=0)
    assert [m[0] for m in env_sent] == ["maria@example.org", "lena@example.org"]
    assert any(
        r["event"] == "test.audit" and r.get("who") == "maria" for r in log_records
    )
    assert env.throttle.snapshot().hits["pw:maria@example.org"] == (T0,), (
        "the charge is stamped with the Env's clock, not the wall's"
    )


async def test_a_send_the_mailer_reports_failed_is_counted_not_raised(env):
    failing = FailingMailer()
    report = await effects.run([MAIL, MAIL], env.with_(mailer=failing))
    assert report == EffectReport(mailed=0, failed=2)
    assert len(failing.sent) == 2, "both were attempted"


async def test_a_mailer_that_raises_costs_one_failure_and_the_rest_still_run(
    env, log_records
):
    class Exploding(RecordingMailer):
        async def send(self, to, subject, body):
            if to == "boom@example.org":
                raise RuntimeError("smtp down")
            return await super().send(to, subject, body)

    mailer = Exploding()
    report = await effects.run(
        [SendMail("boom@example.org", "x", "y"), MAIL, ThrottleHit("pw:a")],
        env.with_(mailer=mailer),
    )
    assert report == EffectReport(mailed=1, failed=1)
    assert [m[0] for m in mailer.sent] == ["maria@example.org"]
    assert env.throttle.snapshot().hits, "the effect after the failure still ran"
    assert any(
        r["event"] == "effects.failed" and r["effect"] == "SendMail"
        for r in log_records
    )


async def test_an_unknown_throttle_family_is_a_failure_not_a_crash(env, log_records):
    report = await effects.run([ThrottleHit("mystery:1")], env)
    assert report == EffectReport(mailed=0, failed=1)
    assert any(r["event"] == "effects.failed" for r in log_records)


async def test_nothing_to_do_is_an_empty_report(env):
    assert await effects.run([], env) == EffectReport()


def test_delivered_answers_only_when_mail_was_planned():
    assert delivered([Audit("x")], EffectReport()) is None, "nothing was to be sent"
    assert delivered([MAIL], EffectReport(mailed=1)) is True
    assert delivered([MAIL, MAIL], EffectReport(mailed=1, failed=1)) is False
    assert delivered([MAIL], EffectReport(mailed=0, failed=1)) is False
