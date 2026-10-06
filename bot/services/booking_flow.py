"""Общая логика: бронь слота и выставление счёта."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, time

from aiogram import Bot
from aiogram.types import LabeledPrice
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.database.repository import book_slot, cancel_appointment, upsert_user
from bot.utils import clip, format_day, format_duration, format_price, format_time, h

logger = logging.getLogger(__name__)

BOOKING_ERRORS = {
    "no_service": "Услуга не найдена. Откройте календарь ещё раз.",
    "bad_date": "Эта дата недоступна.",
    "unfit": "Услуга не помещается в выбранное время. Возьмите более ранний слот.",
    "past": "Это время уже прошло.",
    "missing_slot": "На эту дату нет такого окна. Обновите календарь.",
    "taken": "Это время только что заняли. Выберите другое.",
    "closed": "Это время закрыто. Выберите другое.",
    "busy": "Не удалось закрепить время. Откройте календарь и попробуйте ещё раз.",
}


@dataclass(frozen=True)
class InvoiceOutcome:
    ok: bool
    message: str
    appointment_id: int | None = None


def booking_error_text(code: str) -> str:
    return BOOKING_ERRORS.get(code, "Не получилось записаться. Выберите другое время.")


async def create_booking_and_invoice(
    session: AsyncSession,
    bot: Bot,
    *,
    user_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
    service_id: int,
    day: date,
    slot_time: time,
) -> InvoiceOutcome:
    if not settings.payment_provider_token:
        return InvoiceOutcome(False, "Оплата не настроена. Добавьте PAYMENT_PROVIDER_TOKEN.")

    await upsert_user(
        session,
        user_id=user_id,
        username=username,
        first_name=(first_name[:255] if first_name else None),
        last_name=(last_name[:255] if last_name else None),
        is_active=True,
    )
    try:
        result = await book_slot(
            session,
            user_id=user_id,
            service_id=service_id,
            day=day,
            slot_time=slot_time,
        )
    except Exception:
        logger.exception("Booking failed for user %s", user_id)
        return InvoiceOutcome(False, "Не получилось сохранить запись. Попробуйте ещё раз.")

    if not result.ok or result.appointment_id is None or result.day is None or result.start is None or result.end is None:
        return InvoiceOutcome(False, booking_error_text(result.code))

    summary = (
        "<b>Проверьте запись и оплатите счёт.</b>\n\n"
        f"{h(result.service_name)}\n"
        f"{h(format_day(result.day, with_weekday=True))}\n"
        f"{format_time(result.start)}–{format_time(result.end)}\n"
        f"{format_duration(result.duration_minutes)} · {format_price(result.price)}"
    )
    await bot.send_message(user_id, summary)
    try:
        await bot.send_invoice(
            chat_id=user_id,
            title=clip(result.service_name, 32),
            description=clip(
                f"{format_day(result.day)} в {format_time(result.start)}–{format_time(result.end)}. "
                f"Длительность {format_duration(result.duration_minutes)}.",
                255,
            ),
            payload=str(result.appointment_id),
            provider_token=settings.payment_provider_token,
            currency="RUB",
            prices=[LabeledPrice(label=clip(result.service_name, 128), amount=result.price * 100)],
            start_parameter=f"appt-{result.appointment_id}",
        )
    except Exception:
        logger.exception("send_invoice failed for appointment %s", result.appointment_id)
        code = await cancel_appointment(session, result.appointment_id, actor_id=user_id, as_admin=False)
        if code != "ok":
            logger.error("Could not release appointment %s after invoice error: %s", result.appointment_id, code)
        return InvoiceOutcome(False, "Не удалось выставить счёт, время снова свободно. Попробуйте ещё раз.")

    if settings.test_payments:
        await bot.send_message(
            user_id,
            "Тестовая карта: <code>4242 4242 4242 4242</code>\nСрок — любой в будущем, CVC — любые 3 цифры.",
        )
    logger.info("Invoice sent for appointment %s user %s", result.appointment_id, user_id)
    return InvoiceOutcome(True, "Счёт отправлен в чат.", result.appointment_id)


def parse_book_start(args: str | None) -> tuple[int, date, time] | None:
    """Deep link: book_<service>_<YYYYMMDD>_<HHMM>."""
    if not args:
        return None
    raw = args.strip()
    if not raw.startswith("book_"):
        return None
    parts = raw.split("_")
    if len(parts) != 4:
        return None
    _, service_text, day_text, time_text = parts
    if not (service_text.isdigit() and len(day_text) == 8 and day_text.isdigit() and len(time_text) == 4 and time_text.isdigit()):
        return None
    try:
        service_id = int(service_text)
        day = date(int(day_text[:4]), int(day_text[4:6]), int(day_text[6:8]))
        slot_time = time(int(time_text[:2]), int(time_text[2:4]))
    except ValueError:
        return None
    return service_id, day, slot_time


def encode_book_start(service_id: int, day: date, slot_time: time) -> str:
    return f"book_{service_id}_{day.strftime('%Y%m%d')}_{slot_time.strftime('%H%M')}"
