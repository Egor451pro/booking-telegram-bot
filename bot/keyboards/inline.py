"""Инлайн-клавиатуры админки и отмены записи."""

from __future__ import annotations

from datetime import date

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.database.repository import AppointmentCard, DayAdminView, SlotAdminView
from bot.utils import WEEKDAYS_SHORT, format_time


class AdminCB(CallbackData, prefix="a"):
    action: str
    extra: str = "0"


class UserCB(CallbackData, prefix="u"):
    action: str
    appointment_id: int = 0


def menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Записи на сегодня", callback_data=AdminCB(action="today", extra="0").pack())],
            [InlineKeyboardButton(text="Ближайшие дни", callback_data=AdminCB(action="soon", extra="0").pack())],
            [InlineKeyboardButton(text="Слоты и выходные", callback_data=AdminCB(action="days", extra="0").pack())],
        ]
    )


def appointments_kb(scope: str, cards: list[AppointmentCard], page: int, total: int, page_size: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for card in cards:
        label = f"Отменить {format_time(card.start)} {card.day.strftime('%d.%m')}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=AdminCB(action="ask", extra=f"{card.id}_{scope}_{page}").pack(),
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="←", callback_data=AdminCB(action=scope, extra=str(page - 1)).pack()))
    if (page + 1) * page_size < total:
        nav.append(InlineKeyboardButton(text="→", callback_data=AdminCB(action=scope, extra=str(page + 1)).pack()))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="В меню", callback_data=AdminCB(action="menu").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_cancel_kb(appointment_id: int, scope: str, page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, отменить",
                    callback_data=AdminCB(action="drop", extra=f"{appointment_id}_{scope}_{page}").pack(),
                )
            ],
            [InlineKeyboardButton(text="Назад", callback_data=AdminCB(action=scope, extra=str(page)).pack())],
        ]
    )


def days_kb(days: list[DayAdminView], page: int, page_size: int = 7) -> InlineKeyboardMarkup:
    chunk = days[page * page_size : (page + 1) * page_size]
    rows: list[list[InlineKeyboardButton]] = []
    for item in chunk:
        weekday = WEEKDAYS_SHORT[item.day.weekday()]
        if item.free == 0 and item.booked == 0:
            label = f"{weekday} {item.day.strftime('%d.%m')} · закрыт"
        else:
            label = f"{weekday} {item.day.strftime('%d.%m')} · своб. {item.free}"
        rows.append(
            [InlineKeyboardButton(text=label, callback_data=AdminCB(action="day", extra=item.day.isoformat()).pack())]
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="←", callback_data=AdminCB(action="days", extra=str(page - 1)).pack()))
    if (page + 1) * page_size < len(days):
        nav.append(InlineKeyboardButton(text="→", callback_data=AdminCB(action="days", extra=str(page + 1)).pack()))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="В меню", callback_data=AdminCB(action="menu").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def day_kb(day: date, slots: list[SlotAdminView]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    for slot in slots:
        if slot.state == "blocked":
            label = f"{format_time(slot.start)} ✕"
        elif slot.state == "booked":
            label = f"{format_time(slot.start)} •"
        else:
            label = format_time(slot.start)
        pair.append(InlineKeyboardButton(text=label, callback_data=AdminCB(action="slot", extra=str(slot.id)).pack()))
        if len(pair) == 3:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    iso = day.isoformat()
    rows.append(
        [
            InlineKeyboardButton(text="Закрыть день", callback_data=AdminCB(action="close", extra=iso).pack()),
            InlineKeyboardButton(text="Открыть день", callback_data=AdminCB(action="open", extra=iso).pack()),
        ]
    )
    rows.append([InlineKeyboardButton(text="К датам", callback_data=AdminCB(action="days", extra="0").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def my_appointments_kb(cards: list[AppointmentCard]) -> InlineKeyboardMarkup | None:
    if not cards:
        return None
    rows = [
        [
            InlineKeyboardButton(
                text=f"Отменить {card.day.strftime('%d.%m')} {format_time(card.start)}",
                callback_data=UserCB(action="ask", appointment_id=card.id).pack(),
            )
        ]
        for card in cards
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def user_confirm_kb(appointment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Да, отменить", callback_data=UserCB(action="yes", appointment_id=appointment_id).pack())],
            [InlineKeyboardButton(text="Оставить", callback_data=UserCB(action="no").pack())],
        ]
    )
