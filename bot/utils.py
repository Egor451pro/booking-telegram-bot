"""Форматирование дат и цен для сообщений бота."""

from __future__ import annotations

from datetime import date, time
from html import escape

MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
WEEKDAYS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)
WEEKDAYS_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def h(value: object) -> str:
    return escape("" if value is None else str(value))


def format_price(amount: int) -> str:
    return f"{amount:,}".replace(",", "\u00a0") + "\u00a0₽"


def format_day(value: date, *, with_weekday: bool = False) -> str:
    text = f"{value.day} {MONTHS[value.month - 1]}"
    if with_weekday:
        return f"{WEEKDAYS[value.weekday()]}, {text}"
    return text


def format_time(value: time) -> str:
    return value.strftime("%H:%M")


def format_duration(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f"{hours} ч {rest} мин"
    if hours:
        return f"{hours} ч"
    return f"{rest} мин"


def clip(value: str, limit: int) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"
