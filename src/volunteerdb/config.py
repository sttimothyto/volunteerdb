from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    port: int = 8080
    reload: bool = False
    # AUDIT (default) logs writes, auth events, and problems; INFO adds reads
    # and one line per HTTP request; DEBUG adds query params and asset requests.
    log_level: str = "AUDIT"
    log_file: str = ""  # empty: stderr only (journald in production)
    # IANA zone the parish lives in: date-typed things like planning deadlines
    # mean "end of that day HERE", not in UTC (the container's clock).
    timezone: str = "America/Toronto"
    smtp2go_api_key: str = ""  # empty: emails are printed to the log, not sent
    mail_from: str = "no-reply@sttimothyto.org"
    mail_from_name: str = "VolunteerDB"
    # URL of the decorated roster-template Google Sheet in the Drive folder.
    # Set: the /import page links there instead of offering the bare CSV.
    # Empty (dev): the page falls back to a plain CSV download.
    template_sheet_url: str = ""
    # Built HTML manual served at /manual (signed-in users). Relative paths
    # resolve against the cwd (repo root in dev); the container bakes the
    # docs in and sets VDB_DOCS_DIR=/app/docs-html.
    docs_dir: str = "docs/_build/html"


@lru_cache
def settings() -> Settings:
    return Settings()
