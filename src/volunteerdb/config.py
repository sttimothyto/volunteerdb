from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VDB_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://volunteerdb:volunteerdb@localhost:5432/volunteerdb"
    storage_secret: str = "dev-secret-change-me"
    host: str = "0.0.0.0"
    port: int = 8080
    reload: bool = False


@lru_cache
def settings() -> Settings:
    return Settings()
