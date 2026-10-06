"""Настройки приложения. Секреты читаются только из окружения или .env."""

from __future__ import annotations

import re
from functools import cached_property
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent
_ENV_FILE = ROOT_DIR / ".env"


def _parse_hhmm(value: str) -> tuple[int, int]:
    hours_text, minutes_text = value.split(":", maxsplit=1)
    hours, minutes = int(hours_text), int(minutes_text)
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise ValueError(f"invalid time: {value}")
    return hours, minutes


class Settings(BaseSettings):
    bot_token: str
    admin_ids: str = ""
    studio_name: str = "Студия"

    database_url: str
    db_ssl: bool = True
    # На Windows антивирус часто подменяет сертификат → CERTIFICATE_VERIFY_FAILED.
    # Локально можно false; на Render/Koyeb оставляйте true.
    db_ssl_verify: bool = True

    mode: str = "webhook"
    webhook_base_url: str = ""
    webhook_secret: str = ""
    webapp_url: str = ""
    payment_provider_token: str = ""

    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    timezone: str = "Europe/Moscow"

    work_start: str = "10:00"
    work_end: str = "20:00"
    slot_step_minutes: int = Field(default=30, ge=15, le=120)
    schedule_days: int = Field(default=21, ge=1, le=60)
    hold_minutes: int = Field(default=15, ge=5, le=180)

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE) if _ENV_FILE.exists() else None,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @field_validator(
        "bot_token",
        "admin_ids",
        "studio_name",
        "database_url",
        "webhook_base_url",
        "webhook_secret",
        "webapp_url",
        "payment_provider_token",
        "timezone",
        "work_start",
        "work_end",
        mode="before",
    )
    @classmethod
    def _strip(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("database_url")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        if value.startswith("postgres://"):
            value = "postgresql://" + value[len("postgres://") :]
        if value.startswith("postgresql://") and not value.startswith("postgresql+asyncpg://"):
            value = "postgresql+asyncpg://" + value[len("postgresql://") :]
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must be a PostgreSQL URL")
        return value

    @field_validator("mode", mode="before")
    @classmethod
    def _normalize_mode(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("mode")
    @classmethod
    def _check_mode(cls, value: str) -> str:
        if value not in {"webhook", "polling"}:
            raise ValueError("MODE must be webhook or polling")
        return value

    @field_validator("webhook_secret")
    @classmethod
    def _webhook_secret(cls, value: str) -> str:
        if value and not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value):
            raise ValueError("WEBHOOK_SECRET may contain only letters, digits, underscore and hyphen")
        return value

    @field_validator("studio_name")
    @classmethod
    def _studio_name(cls, value: str) -> str:
        return value or "Студия"

    @field_validator("webhook_base_url", "webapp_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def _check_runtime(self) -> Settings:
        if self.mode == "webhook":
            if not self.webhook_base_url:
                raise ValueError("WEBHOOK_BASE_URL is required when MODE=webhook")
            if not self.webhook_secret:
                raise ValueError("WEBHOOK_SECRET is required when MODE=webhook")
        if self.webhook_base_url and not self.webhook_base_url.startswith("https://"):
            raise ValueError("WEBHOOK_BASE_URL must start with https://")
        if self.webapp_url and not self.webapp_url.startswith("https://"):
            raise ValueError("WEBAPP_URL must start with https://")
        start = _parse_hhmm(self.work_start)
        end = _parse_hhmm(self.work_end)
        if start >= end:
            raise ValueError("WORK_START must be earlier than WORK_END")
        if self.slot_step_minutes <= 0 or (24 * 60) % self.slot_step_minutes != 0:
            raise ValueError("SLOT_STEP_MINUTES must divide 24 hours")
        # Проверяем, что часовой пояс существует, не импортируя его в каждом хэндлере.
        from zoneinfo import ZoneInfo

        ZoneInfo(self.timezone)
        return self

    @cached_property
    def admin_id_set(self) -> frozenset[int]:
        ids: list[int] = []
        for part in self.admin_ids.replace(";", ",").split(","):
            item = part.strip()
            if not item:
                continue
            try:
                ids.append(int(item))
            except ValueError as exc:
                raise ValueError("ADMIN_IDS must be comma-separated Telegram IDs") from exc
        return frozenset(ids)

    @property
    def webhook_path(self) -> str:
        """Путь webhook: /webhook/<WEBHOOK_SECRET> — секрет в URL будит Render после сна."""
        if self.webhook_secret:
            return f"/webhook/{self.webhook_secret}"
        return "/webhook"

    @property
    def webhook_url(self) -> str:
        return f"{self.webhook_base_url}{self.webhook_path}"

    @property
    def test_payments(self) -> bool:
        return ":TEST:" in self.payment_provider_token


settings = Settings()
