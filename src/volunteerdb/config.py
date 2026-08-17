from datetime import time
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Accepted VDB_LOG_LEVEL values. Defined here rather than in log.py because
# the validator below needs them and log.py already imports this module;
# log.py owns the mapping from these names to numeric levels.
LOG_LEVELS = ("DEBUG", "INFO", "AUDIT", "WARNING", "ERROR")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VDB_", env_file=".env", extra="ignore"
    )

    database_url: str = (
        "postgresql+asyncpg://volunteerdb:volunteerdb@localhost:5432/volunteerdb"
    )
    storage_secret: str = (
        ""  # empty: ephemeral per-boot secret (dev only) — set in production
    )
    cookie_secure: bool = (
        False  # true when served over HTTPS: adds Secure to the session cookie
    )
    host: str = "0.0.0.0"
    port: int = Field(default=8080, gt=0, lt=65536)
    reload: bool = False
    # AUDIT (default) logs writes, auth events, and problems; INFO adds reads
    # and one line per HTTP request; DEBUG adds query params and asset requests.
    log_level: str = "AUDIT"
    log_file: str = ""  # empty: stderr only (journald in production)
    # IANA zone the parish lives in: date-typed things like election deadlines
    # mean "end of that day HERE", not in UTC (the container's clock).
    timezone: str = "America/Toronto"
    smtp2go_api_key: str = ""  # empty: emails are printed to the log, not sent
    mail_from: str = "no-reply@sttimothyto.org"
    mail_from_name: str = "VolunteerDB"
    # Absolute origin for links in nightly-job emails (e.g.
    # https://vdb.sttimothyto.org). UI-sent mail derives links from the live
    # request instead; empty means job emails simply carry no link.
    public_base_url: str = ""
    # How many days ahead the nightly digest (jobs/event_reminders.py)
    # reminds people of events they are scheduled to serve at.
    event_reminder_days: int = Field(default=3, gt=0)
    # In-app scheduler (volunteerdb.scheduler) driving the nightly jobs
    # below. Forced off under VDB_RELOAD regardless: dev reload restarts the
    # process on every save, which would re-fire startup hooks.
    scheduler_enabled: bool = True
    # Where scheduler job failures are emailed; empty: they only log (ERROR).
    alert_email: str = ""
    # Parish-local (VDB_TIMEZONE) times the nightly jobs run, kept clear of
    # 02:00-02:30 when the host's backup and Drive-sync timers fire. Setting
    # one a couple of minutes ahead is the way to watch a job fire in dev.
    fetch_pages_at: time = time(3, 0)
    proposal_digest_at: time = time(3, 30)
    event_reminders_at: time = time(4, 0)
    # How long an invite link — which is also the password-reset link — stays
    # usable. NIST SP 800-63B §4.2.1.2 would cap an emailed recovery code at
    # 24 hours; a week is a deliberate deviation for a parish where invitees
    # read email weekly. The risk expiry bounds is small — the link grants a
    # fresh account or a password reset on an account whose fallback sign-in
    # is an emailed code anyway — and expiry is never a lockout: the account
    # can still sign in with an emailed code and set a password from /account.
    invite_ttl_hours: int = Field(default=168, gt=0)
    # URL of the decorated roster-template Google Sheet in the Drive folder.
    # Set: the /import page links there instead of offering the bare CSV.
    # Empty (dev): the page falls back to a plain CSV download.
    template_sheet_url: str = ""
    # Built HTML manual served at /manual (signed-in users). Relative paths
    # resolve against the cwd (repo root in dev); the container bakes the
    # docs in and sets VDB_DOCS_DIR=/app/docs-html.
    docs_dir: str = "docs/_build/html"

    # The validators below exist so a typo fails at startup, naming the
    # variable, rather than surviving into the first request or the first
    # nightly job that happens to need the value.

    @field_validator("log_level")
    @classmethod
    def _known_log_level(cls, v: str) -> str:
        level = v.strip().upper()
        if level not in LOG_LEVELS:
            raise ValueError(f"must be one of {', '.join(LOG_LEVELS)}")
        return level

    @field_validator("timezone")
    @classmethod
    def _real_timezone(cls, v: str) -> str:
        # Previously surfaced only at the first ZoneInfo() call — an election
        # page view, or a nightly job firing at 03:00.
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"not an IANA time zone: {v!r}") from exc
        return v

    @field_validator("mail_from")
    @classmethod
    def _address_shaped(cls, v: str) -> str:
        if v and "@" not in v:
            raise ValueError(f"must be an email address, got {v!r}")
        return v


@lru_cache
def settings() -> Settings:
    return Settings()
