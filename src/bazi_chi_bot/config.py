"""Environment-backed application settings."""

from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        populate_by_name=True,
        extra="ignore",
    )

    bot_token: str = Field(
        min_length=20,
        validation_alias=AliasChoices("TELEGRAM_BOT_TOKEN", "BOT_TOKEN"),
    )
    # Optional second bot.  When omitted the application remains Telegram-only.
    bale_bot_token: str | None = Field(default=None, min_length=20)
    database_path: Path = Path("data/bazi_chi_bot.sqlite3")
    log_level: str = "INFO"
    telegram_proxy_urls: list[str] = Field(default_factory=list)
    bale_api_base_url: str = "https://tapi.bale.ai"
    # Wallet provider token issued by Bale's botfather.  Keep it only in the
    # deployment environment; it must never be committed or shown in logs.
    bale_payment_provider_token: str | None = None
    admin_telegram_ids: str = Field(
        default="", validation_alias=AliasChoices("TELEGRAM_ADMIN_IDS", "ADMIN_TELEGRAM_IDS")
    )
    payment_reviewer_telegram_ids: str = Field(
        default="1767552952",
        validation_alias=AliasChoices(
            "TELEGRAM_PAYMENT_REVIEWER_IDS", "PAYMENT_REVIEWER_TELEGRAM_IDS"
        ),
    )
    bale_admin_ids: str = ""
    bale_payment_reviewer_ids: str = ""
    countdown_timezone: str = "Asia/Tehran"

    @field_validator("telegram_proxy_urls")
    @classmethod
    def normalize_proxy_urls(cls, urls: list[str]) -> list[str]:
        """Remove blank and duplicate proxy entries while preserving priority."""
        return list(dict.fromkeys(url.strip() for url in urls if url.strip()))

    @field_validator("bale_bot_token", mode="before")
    @classmethod
    def normalize_bale_token(cls, token: str | None) -> str | None:
        """Treat an empty optional env var as disabled."""
        if token is None:
            return None
        token = str(token).strip()
        return token or None

    @field_validator("bale_payment_provider_token", mode="before")
    @classmethod
    def normalize_bale_payment_token(cls, token: str | None) -> str | None:
        if token is None:
            return None
        token = str(token).strip()
        return token or None
