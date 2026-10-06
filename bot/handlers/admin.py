"""Админ-панель: записи, закрытие дней и отдельных слотов."""

from __future__ import annotations

import math
from datetime import date

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.database.models import AppointmentStatus
from bot.database.repository import (
    AppointmentCard,
    admin_counts,
    admin_day_slots,
    admin_days,
    cancel_appointment,
    get_card,
    list_cards,
    set_day_closed,
    toggle_slot,
)
from bot.keyboards.inline import (
    AdminCB,
    appointments_kb,
    confirm_cancel_kb,
    day_kb,
    days_kb,
    menu_kb,
)
from bot.utils import WEEKDAYS_SHORT, format_day, format_price, format_time, h

router = Router(name="admin")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

PAGE_SIZE = 5
DAYS_PAGE = 7
TITLES = {"today": "Записи на сегодня", "soon": "Ближайшие дни"}
TOGGLE_TEXT = {
    "missing": "Слот не найден.",
    "booked": "Это окно занимает запись клиента. Сначала отмените запись.",
    "closed": "Слот закрыт.",
    "opened": "Слот открыт.",
}
DAY_TEXT = {
    "missing": "На эту дату нет сетки.",
    "closed": "День закрыт. Уже созданные записи сохранены.",
    "opened": "Свободные окна открыты. Записи клиентов не трогал.",
}


def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_id_set


def parse_page(extra: str) -> int:
    return int(extra) if extra.isdigit() else 0


def parse_day(extra: str) -> date | None:
    try:
        return date.fromisoformat(extra)
    except ValueError:
        return None


def parse_ref(extra: str) -> tuple[int, str, int] | None:
    parts = extra.split("_")
    if len(parts) != 3 or not parts[0].isdigit() or not parts[2].isdigit():
        return None
    if parts[1] not in TITLES:
        return None
    return int(parts[0]), parts[1], int(parts[2])


async def safe_edit(message: Message, text: str, markup: InlineKeyboardMarkup | None) -> None:
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


def card_html(card: AppointmentCard) -> str:
    status = "ожидает оплаты" if card.status == AppointmentStatus.pending else "оплачена"
    username = f" (@{h(card.username)})" if card.username else ""
    return "\n".join(
        [
            f"<b>{format_time(card.start)}–{format_time(card.end)}</b> · {h(format_day(card.day))}",
            f"{h(card.service_name)} · {format_price(card.price)}",
            f'<a href="tg://user?id={card.user_id}">{h(card.full_name)}</a>{username}',
            status,
        ]
    )


async def menu_text(session: AsyncSession) -> str:
    today_count, soon_count = await admin_counts(session)
    return (
        f"<b>Панель администратора</b>\n{h(settings.studio_name)}\n\n"
        f"Сегодня: <b>{today_count}</b>\n"
        f"Ближайшие дни: <b>{soon_count}</b>"
    )


async def show_menu(message: Message, session: AsyncSession, *, edit: bool) -> None:
    text = await menu_text(session)
    if edit:
        await safe_edit(message, text, menu_kb())
        return
    await message.answer(text, reply_markup=menu_kb())


async def show_appointments(message: Message, session: AsyncSession, scope: str, page: int) -> None:
    page = max(page, 0)
    cards, total = await list_cards(
        session,
        user_id=None,
        scope=scope,
        offset=page * PAGE_SIZE,
        limit=PAGE_SIZE,
    )
    title = TITLES.get(scope, "Записи")
    if not cards:
        text = f"<b>{title}</b>\n\nЗаписей нет."
    else:
        pages = max(1, math.ceil(total / PAGE_SIZE))
        body = "\n\n".join(card_html(card) for card in cards)
        text = f"<b>{title}</b>\n\n{body}\n\nСтраница {page + 1} из {pages}"
    await safe_edit(message, text, appointments_kb(scope, cards, page, total, PAGE_SIZE))


async def show_days(message: Message, session: AsyncSession, page: int) -> None:
    page = max(page, 0)
    days = await admin_days(session)
    if not days:
        await safe_edit(message, "Сетка расписания ещё не создана.", menu_kb())
        return
    max_page = max(0, math.ceil(len(days) / DAYS_PAGE) - 1)
    page = min(page, max_page)
    await safe_edit(
        message,
        "<b>Слоты и выходные</b>\nЧисло — свободные окна сетки. Откройте день, чтобы закрыть время.",
        days_kb(days, page, DAYS_PAGE),
    )


async def show_day(message: Message, session: AsyncSession, day: date) -> None:
    slots = await admin_day_slots(session, day)
    weekday = WEEKDAYS_SHORT[day.weekday()]
    if not slots:
        await safe_edit(message, f"{weekday}, {h(format_day(day))}\n\nНа эту дату нет сетки.", days_kb(await admin_days(session), 0))
        return
    text = (
        f"<b>{weekday}, {h(format_day(day))}</b>\n"
        "• занято клиентом, ✕ закрыто вами.\n"
        "Нажатие на свободное или закрытое окно переключает его."
    )
    await safe_edit(message, text, day_kb(day, slots))


@router.message(Command("admin"))
async def admin_entry(message: Message, session: AsyncSession) -> None:
    if message.from_user is None or not is_admin(message.from_user.id):
        await message.answer("Эта команда только для администратора.")
        return
    await show_menu(message, session, edit=False)


@router.callback_query(AdminCB.filter())
async def on_admin(query: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    if query.from_user is None or not is_admin(query.from_user.id):
        await query.answer("Недостаточно прав", show_alert=True)
        return
    if not isinstance(query.message, Message):
        await query.answer("Сообщение устарело", show_alert=True)
        return
    action = callback_data.action
    extra = callback_data.extra
    if action == "menu":
        await query.answer()
        await show_menu(query.message, session, edit=True)
        return
    if action in TITLES:
        await query.answer()
        await show_appointments(query.message, session, action, parse_page(extra))
        return
    if action == "days":
        await query.answer()
        await show_days(query.message, session, parse_page(extra))
        return
    if action == "day":
        day = parse_day(extra)
        if day is None:
            await query.answer("Кнопка устарела", show_alert=True)
            return
        await query.answer()
        await show_day(query.message, session, day)
        return
    if action == "slot":
        if not extra.isdigit():
            await query.answer("Кнопка устарела", show_alert=True)
            return
        code, day = await toggle_slot(session, int(extra))
        alert = code == "booked"
        await query.answer(TOGGLE_TEXT.get(code, "Готово"), show_alert=alert)
        if day is not None and code != "booked":
            await show_day(query.message, session, day)
        return
    if action in {"close", "open"}:
        day = parse_day(extra)
        if day is None:
            await query.answer("Кнопка устарела", show_alert=True)
            return
        code = await set_day_closed(session, day, closed=action == "close")
        await query.answer(DAY_TEXT.get(code, "Готово"), show_alert=code == "missing")
        if code != "missing":
            await show_day(query.message, session, day)
        return
    if action == "ask":
        ref = parse_ref(extra)
        if ref is None:
            await query.answer("Кнопка устарела", show_alert=True)
            return
        appointment_id, scope, page = ref
        card = await get_card(session, appointment_id)
        if card is None:
            await query.answer("Запись не найдена", show_alert=True)
            return
        note = ""
        if card.status == AppointmentStatus.confirmed:
            note = "\n\nОплата не возвращается автоматически."
        text = f"<b>Отменить запись?</b>\n\n{card_html(card)}{note}"
        await query.answer()
        await safe_edit(query.message, text, confirm_cancel_kb(appointment_id, scope, page))
        return
    if action == "drop":
        ref = parse_ref(extra)
        if ref is None:
            await query.answer("Кнопка устарела", show_alert=True)
            return
        appointment_id, scope, page = ref
        code = await cancel_appointment(session, appointment_id, actor_id=query.from_user.id, as_admin=True)
        if code == "ok":
            await query.answer("Запись отменена")
        else:
            await query.answer("Не удалось отменить", show_alert=True)
        await show_appointments(query.message, session, scope, page)
        return
    await query.answer("Кнопка устарела", show_alert=True)
