"""Environment-backed application settings."""

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    bot_token: str = Field(min_length=20)
    database_path: Path = Path("data/bazi_chi_bot.sqlite3")
    log_level: str = "INFO"
    telegram_proxy_urls: list[str] = Field(default_factory=list)
    admin_telegram_ids: str = ""
    payment_reviewer_telegram_ids: str = "1767552952"
    countdown_timezone: str = "Asia/Tehran"

    @field_validator("telegram_proxy_urls")
    @classmethod
    def normalize_proxy_urls(cls, urls: list[str]) -> list[str]:
        """Remove blank and duplicate proxy entries while preserving priority."""
        return list(dict.fromkeys(url.strip() for url in urls if url.strip()))
