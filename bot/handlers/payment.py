"""Проверка счёта перед оплатой и подтверждение записи после неё."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import Message, PreCheckoutQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.repository import PaymentView, check_pre_checkout, confirm_payment
from bot.utils import format_day, format_price, format_time, h

logger = logging.getLogger(__name__)
router = Router(name="payment")


def _appointment_id(payload: str) -> int | None:
    return int(payload) if payload.isdigit() else None


def success_text(view: PaymentView) -> str:
    if view.state == "lost":
        return (
            "Оплата прошла, но это время уже занято.\n"
            f"Напишите администратору и укажите ID платежа: <code>{h(view.charge_id)}</code>"
        )
    if view.state in {"missing", "rejected"} or view.day is None or view.start is None or view.end is None:
        return (
            "Оплата получена, но запись не найдена. "
            f"ID платежа: <code>{h(view.charge_id)}</code>"
        )
    return (
        "<b>Вы записаны.</b>\n\n"
        f"{h(view.service_name)}\n"
        f"{h(format_day(view.day, with_weekday=True))}\n"
        f"{format_time(view.start)}–{format_time(view.end)}\n"
        f"{format_price(view.price)}"
    )


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, session: AsyncSession) -> None:
    appointment_id = _appointment_id(query.invoice_payload)
    if appointment_id is None or query.from_user is None:
        logger.warning("pre_checkout rejected: bad payload=%r", query.invoice_payload)
        await query.answer(ok=False, error_message="Счёт недействителен.")
        return
    try:
        error = await check_pre_checkout(session, appointment_id, query.from_user.id)
    except Exception:
        logger.exception("pre_checkout failed for appointment %s", appointment_id)
        await query.answer(ok=False, error_message="Не удалось проверить запись. Попробуйте ещё раз.")
        return
    if error:
        logger.warning(
            "pre_checkout denied appointment=%s user=%s reason=%s",
            appointment_id,
            query.from_user.id,
            error,
        )
        await query.answer(ok=False, error_message=error[:200])
        return
    logger.info("pre_checkout ok appointment=%s user=%s", appointment_id, query.from_user.id)
    await query.answer(ok=True)


@router.message(F.successful_payment, F.chat.type == "private")
async def successful_payment(message: Message, session: AsyncSession) -> None:
    payment = message.successful_payment
    if payment is None or message.from_user is None:
        return
    appointment_id = _appointment_id(payment.invoice_payload)
    if appointment_id is None:
        logger.error("Successful payment with unexpected payload")
        return
    try:
        view = await confirm_payment(
            session,
            appointment_id,
            message.from_user.id,
            payment.telegram_payment_charge_id,
        )
    except Exception:
        logger.exception("Failed to confirm payment for appointment %s", appointment_id)
        await message.answer(
            "Оплата получена, но запись не сохранилась. "
            f"ID платежа: <code>{h(payment.telegram_payment_charge_id)}</code>"
        )
        return
    if view.state == "already":
        return
    logger.info("Payment %s for appointment %s state=%s", payment.telegram_payment_charge_id, appointment_id, view.state)
    await message.answer(success_text(view))
