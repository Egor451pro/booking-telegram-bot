"""Клиентские команды, календарь из Web App и список своих записей."""

from __future__ import annotations

import json
import logging
from datetime import date, time

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.database.models import AppointmentStatus
from bot.database.repository import (
    AppointmentCard,
    cancel_appointment,
    get_card,
    list_cards,
    upsert_user,
)
from bot.keyboards.inline import UserCB, my_appointments_kb, user_confirm_kb
from bot.keyboards.reply import BOOK_BUTTON, MY_BUTTON, main_menu
from bot.services.booking_flow import create_booking_and_invoice, parse_book_start
from bot.utils import format_day, format_price, format_time, h

logger = logging.getLogger(__name__)
router = Router(name="client")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

USER_LIMIT = 10
CANCEL_TEXT = {
    "ok": "Запись отменена.",
    "not_found": "Запись не найдена.",
    "forbidden": "Это не ваша запись.",
    "past": "Эту запись уже нельзя отменить в боте.",
    "already": "Запись уже отменена.",
}


class WebAppBooking(BaseModel):
    service_id: int = Field(gt=0)
    date: str
    slot_time: str

    def parsed_date(self) -> date:
        return date.fromisoformat(self.date)

    def parsed_time(self) -> time:
        return time.fromisoformat(self.slot_time).replace(second=0, microsecond=0)


async def remember_user(message: Message, session: AsyncSession) -> None:
    user = message.from_user
    if user is None:
        return
    first_name = (user.first_name or None)
    if first_name:
        first_name = first_name[:255]
    last_name = (user.last_name or None)
    if last_name:
        last_name = last_name[:255]
    username = user.username[:64] if user.username else None
    await upsert_user(
        session,
        user_id=user.id,
        username=username,
        first_name=first_name,
        last_name=last_name,
        is_active=True,
    )


def parse_service_arg(args: str | None) -> int | None:
    if not args:
        return None
    raw = args.strip()
    for prefix in ("service_", "service"):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :]
            break
    else:
        return None
    return int(raw) if raw.isdigit() else None


def welcome_text(name: str) -> str:
    if settings.webapp_url:
        how = "Нажмите «Записаться», выберите услугу и время, затем оплатите счёт в чате."
    else:
        how = "Календарь ещё не подключён: в настройках бота не указан WEBAPP_URL."
    return f"Здравствуйте, {h(name)}.\n\n<b>{h(settings.studio_name)}</b> — запись на услуги. {how}"


def my_text(cards: list[AppointmentCard], total: int) -> str:
    if not cards:
        return "Активных записей нет. Нажмите «Записаться», чтобы выбрать время."
    blocks = ["<b>Ваши записи</b>", ""]
    for card in cards:
        status = "ожидает оплаты" if card.status == AppointmentStatus.pending else "оплачена"
        blocks.append(
            "\n".join(
                [
                    f"<b>{h(card.service_name)}</b>",
                    h(format_day(card.day, with_weekday=True)),
                    f"{format_time(card.start)}–{format_time(card.end)} · {format_price(card.price)}",
                    status,
                    "",
                ]
            )
        )
    if total > len(cards):
        blocks.append("Показаны ближайшие 10.")
    return "\n".join(blocks).strip()


async def show_my(message: Message, session: AsyncSession, *, edit: bool) -> None:
    user = message.from_user
    if user is None:
        return
    cards, total = await list_cards(session, user_id=user.id, scope="mine", offset=0, limit=USER_LIMIT)
    text = my_text(cards, total)
    markup = my_appointments_kb(cards)
    if edit:
        try:
            await message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise
        return
    await message.answer(text, reply_markup=markup)


@router.message(CommandStart())
async def start(message: Message, command: CommandObject, session: AsyncSession, bot: Bot) -> None:
    try:
        await remember_user(message, session)
    except Exception:
        logger.exception("Failed to save user %s", message.from_user.id if message.from_user else None)
        await message.answer("Не получилось открыть профиль. Попробуйте ещё раз через минуту.")
        return

    book = parse_book_start(command.args)
    if book is not None and message.from_user is not None:
        service_id, day, slot_time = book
        outcome = await create_booking_and_invoice(
            session,
            bot,
            user_id=message.from_user.id,
            username=message.from_user.username,
            first_name=(message.from_user.first_name or None),
            last_name=(message.from_user.last_name or None),
            service_id=service_id,
            day=day,
            slot_time=slot_time,
        )
        if not outcome.ok:
            await message.answer(outcome.message, reply_markup=main_menu())
            return
        await message.answer("Если счёт не видно выше — откройте чат ещё раз.", reply_markup=main_menu())
        return

    name = message.from_user.full_name if message.from_user else "друг"
    await message.answer(welcome_text(name), reply_markup=main_menu(parse_service_arg(command.args)))


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    text = (
        "<b>Как записаться</b>\n"
        "1. Нажмите «Записаться».\n"
        "2. Выберите услугу, день и время.\n"
        "3. Оплатите счёт в чате. Пока счёт не оплачен, время держится "
        f"{settings.hold_minutes} мин.\n\n"
        "/my — ваши записи\n"
        "/book — показать кнопку календаря"
    )
    if settings.test_payments:
        text += "\n\nТестовая карта: <code>4242 4242 4242 4242</code>\nСрок — любой в будущем, CVC — любые 3 цифры."
    await message.answer(text, reply_markup=main_menu())


@router.message(Command("book"))
@router.message(F.text == BOOK_BUTTON)
async def book_command(message: Message, session: AsyncSession) -> None:
    await remember_user(message, session)
    if not settings.webapp_url:
        await message.answer("Календарь не подключён. Нужен HTTPS-адрес в WEBAPP_URL.")
        return
    await message.answer("Откройте календарь кнопкой ниже.", reply_markup=main_menu())


@router.message(Command("my"))
@router.message(F.text == MY_BUTTON)
async def my_command(message: Message, session: AsyncSession) -> None:
    await remember_user(message, session)
    await show_my(message, session, edit=False)


@router.message(F.web_app_data)
async def on_webapp(message: Message, session: AsyncSession, bot: Bot) -> None:
    if message.web_app_data is None or message.from_user is None:
        return
    try:
        payload = WebAppBooking.model_validate(json.loads(message.web_app_data.data))
        day = payload.parsed_date()
        slot_time = payload.parsed_time()
    except (json.JSONDecodeError, ValidationError, ValueError):
        await message.answer("Не удалось прочитать данные календаря. Откройте запись ещё раз.")
        return
    outcome = await create_booking_and_invoice(
        session,
        bot,
        user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=(message.from_user.first_name or None),
        last_name=(message.from_user.last_name or None),
        service_id=payload.service_id,
        day=day,
        slot_time=slot_time,
    )
    if not outcome.ok:
        await message.answer(outcome.message)


@router.callback_query(UserCB.filter())
async def on_user_callback(query: CallbackQuery, callback_data: UserCB, session: AsyncSession) -> None:
    if not isinstance(query.message, Message) or query.from_user is None:
        await query.answer()
        return
    if callback_data.action == "no":
        await query.answer()
        await show_my(query.message, session, edit=True)
        return
    if callback_data.action == "ask":
        card = await get_card(session, callback_data.appointment_id, user_id=query.from_user.id)
        if card is None or card.status == AppointmentStatus.cancelled:
            await query.answer("Запись не найдена", show_alert=True)
            return
        warning = ""
        if card.status == AppointmentStatus.confirmed:
            warning = "\n\nТестовая оплата не возвращается автоматически."
        text = (
            "<b>Отменить запись?</b>\n\n"
            f"{h(card.service_name)}\n"
            f"{h(format_day(card.day, with_weekday=True))}\n"
            f"{format_time(card.start)}–{format_time(card.end)}"
            f"{warning}"
        )
        await query.answer()
        await query.message.edit_text(text, reply_markup=user_confirm_kb(card.id))
        return
    if callback_data.action == "yes":
        code = await cancel_appointment(
            session,
            callback_data.appointment_id,
            actor_id=query.from_user.id,
            as_admin=False,
        )
        await query.answer(CANCEL_TEXT.get(code, "Не удалось отменить"), show_alert=code != "ok")
        await show_my(query.message, session, edit=True)
        return
    await query.answer()


@router.message(F.text)
async def fallback(message: Message) -> None:
    if message.text and message.text.startswith("/"):
        await message.answer("Неизвестная команда. Список: /help")
        return
    await message.answer("Откройте календарь кнопкой «Записаться» или посмотрите визиты: /my")
