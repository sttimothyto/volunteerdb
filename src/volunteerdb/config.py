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
    # The organisation this instance serves, e.g. "St. Timothy's". Appears in
    # outbound mail, and its name and mail domain become context-specific
    # terms in the password policy (passwords.py). Empty by default rather
    # than a placeholder: the mail copy drops the clause cleanly when it is
    # unset, where a default of "VolunteerDB" would produce "Your VolunteerDB
    # account at VolunteerDB" on every instance that forgot to set it.
    org_name: str = ""
    smtp2go_api_key: str = ""  # empty: emails are logged, not sent
    # With no API key, print the whole message body (sign-in codes, invite and
    # address-change links included) instead of just a warning. Implied by
    # VDB_RELOAD, so `make dev` needs nothing set. Leave it off anywhere real,
    # or a forgotten API key writes every credential the app issues into the
    # process log.
    debug_mail: bool = False
    # RFC 2606 reserved, so an instance that never set this is obvious in the
    # logs and cannot deliver anywhere real. Production sets it from the site
    # file; the address must be on a domain the mail provider is authorised
    # to send for.
    mail_from: str = "no-reply@example.invalid"
    mail_from_name: str = "VolunteerDB"
    # Absolute origin for links in nightly-job emails (e.g.
    # https://vdb.example.org). UI-sent mail derives links from the live
    # request instead; empty means job emails simply carry no link.
    public_base_url: str = ""
    # In-app scheduler (volunteerdb.scheduler) driving the nightly jobs
    # below. Forced off under VDB_RELOAD regardless: dev reload restarts the
    # process on every save, which would re-fire startup hooks.
    scheduler_enabled: bool = True
    # Where scheduler job failures are emailed; empty: they only log (ERROR).
    alert_email: str = ""
    # Who an admin is told to contact when the mail-allowance banner fires
    # (services/mail_quota.py) — whoever set this instance up and can raise
    # the plan or cut the sending. Empty falls back to alert_email, which is
    # already that person on every instance that set it; empty both ways and
    # the banner simply names nobody rather than inventing an address.
    support_contact: str = ""
    # Parish-local (VDB_TIMEZONE) times the nightly jobs run, kept clear of
    # 02:00 when the host's backup timer fires. Setting one a couple of
    # minutes ahead is the way to watch a job fire in dev.
    roster_sync_at: time = time(2, 30)
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
    # URL of the decorated roster-template Google Sheet on Drive, shared
    # read-only to anyone with the link. Set: a team's Roster spreadsheet
    # section links there instead of offering the bare CSV, and a leader who
    # copies it inherits the dropdowns, hidden ID column and header warning.
    # Empty (dev): the section falls back to a plain CSV download.
    template_sheet_url: str = ""
    # The parish Google token: an OAuth client + refresh token authorised as
    # the parish Google account (scripts/google_authorize.py). Named for the
    # roster sheets, which came first, but it serves both Google integrations:
    #   * Sheets roster sync (jobs/roster_sync.py) -- needs the folder id too,
    #     the Drive folder new roster sheets are created in. All four set:
    #     rosters sync with their sheets nightly and the team page can sync
    #     one on demand. Provisioning: docs/how-to/roster-spreadsheets.md.
    #   * Calendar sync (jobs/calendar_sync.py) -- the first three alone: the
    #     job creates the parish calendar itself and keeps its id in
    #     app_setting. Provisioning: docs/how-to/google-calendar-sync.md.
    # Any of the three empty: both jobs exit "not configured".
    sheets_client_id: str = ""
    sheets_client_secret: str = ""
    sheets_refresh_token: str = ""
    sheets_folder_id: str = ""
    # Built HTML manual served at /manual (signed-in users). Relative paths
    # resolve against the cwd (repo root in dev); the container bakes the
    # docs in and sets VDB_DOCS_DIR=/app/docs-html.
    docs_dir: str = "docs/_build/html"
    # The manual's search model (model2vec potion-base-8M, pinned in
    # manual_model.py): `make model` fetches it here, the container bakes it
    # in and sets /app/models/potion-base-8M. Missing: the search box answers
    # on keywords alone, and the log says so once. Empty: keyword-only on
    # purpose, and silent about it.
    manual_model_dir: str = ".models/potion-base-8M"

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
