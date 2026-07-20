from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VDB_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://volunteerdb:volunteerdb@localhost:5432/volunteerdb"
    storage_secret: str = ""  # empty: ephemeral per-boot secret (dev only) — set in production
    cookie_secure: bool = False  # true when served over HTTPS: adds Secure to the session cookie
    host: str = "0.0.0.0"
    port: int = 8080
    reload: bool = False
    smtp2go_api_key: str = ""  # empty: emails are printed to the log, not sent
    mail_from: str = "no-reply@sttimothyto.org"
    mail_from_name: str = "VolunteerDB"


@lru_cache
def settings() -> Settings:
    return Settings()
