"""Reply-клавиатура. Web App открывается только с неё: sendData иначе не работает."""

from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, WebAppInfo

from bot.config import settings

BOOK_BUTTON = "📅 Записаться"
MY_BUTTON = "📋 Мои записи"


def build_webapp_url(service_id: int | None = None) -> str:
    base = settings.webapp_url
    if not base:
        return ""
    url = base if base.endswith(".html") else f"{base}/"
    if service_id is None:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}service={service_id}"


def main_menu(service_id: int | None = None) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []
    url = build_webapp_url(service_id)
    if url:
        rows.append([KeyboardButton(text=BOOK_BUTTON, web_app=WebAppInfo(url=url))])
    rows.append([KeyboardButton(text=MY_BUTTON)])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Запись или мои визиты",
    )
