"""Проверка initData Telegram Web App."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

from bot.config import settings

_MAX_AGE_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class WebAppUser:
    id: int
    username: str | None
    first_name: str | None
    last_name: str | None


class WebAppAuthError(ValueError):
    pass


def parse_webapp_user(init_data: str) -> WebAppUser:
    if not init_data or not init_data.strip():
        raise WebAppAuthError("Нет данных Telegram. Откройте календарь кнопкой «Записаться».")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", None)
    if not received:
        raise WebAppAuthError("Подпись Telegram отсутствует.")

    data_check = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", settings.bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise WebAppAuthError("Подпись Telegram не совпала.")

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError as exc:
        raise WebAppAuthError("Некорректная дата авторизации.") from exc
    if auth_date <= 0 or abs(int(time.time()) - auth_date) > _MAX_AGE_SECONDS:
        raise WebAppAuthError("Сессия календаря устарела. Откройте его снова.")

    raw_user = pairs.get("user")
    if not raw_user:
        raise WebAppAuthError("В данных Telegram нет пользователя.")
    try:
        user = json.loads(raw_user)
        user_id = int(user["id"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise WebAppAuthError("Не удалось прочитать пользователя Telegram.") from exc

    first = str(user.get("first_name") or "").strip()[:255] or None
    last = str(user.get("last_name") or "").strip()[:255] or None
    username = user.get("username")
    if isinstance(username, str):
        username = username[:64]
    else:
        username = None
    return WebAppUser(id=user_id, username=username, first_name=first, last_name=last)
