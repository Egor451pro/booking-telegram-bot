"""Точка входа. FastAPI отдаёт календарь и API, aiogram принимает апдейты."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
from contextlib import asynccontextmanager
from datetime import date, time

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, ErrorEvent, Update
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from bot.config import ROOT_DIR, settings
from bot.database.connection import SessionFactory, dispose_engine, init_models, ping_database
from bot.database.repository import available_starts, calendar_availability, list_services, prepare_database
from bot.handlers import admin, client, payment
from bot.middlewares import DbSessionMiddleware
from bot.schedule import horizon_end, today_local
from bot.services.booking_flow import create_booking_and_invoice
from bot.utils import format_time
from bot.webapp_auth import WebAppAuthError, parse_webapp_user

logger = logging.getLogger(__name__)
bot_username: str = ""

CLIENT_COMMANDS = [
    BotCommand(command="start", description="Начать"),
    BotCommand(command="book", description="Открыть запись"),
    BotCommand(command="my", description="Мои записи"),
    BotCommand(command="help", description="Помощь"),
]
ADMIN_COMMANDS = CLIENT_COMMANDS + [BotCommand(command="admin", description="Панель администратора")]

bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
dp.update.middleware(DbSessionMiddleware(SessionFactory))
dp.include_router(payment.router)
dp.include_router(admin.router)
dp.include_router(client.router)


@dp.error()
async def on_error(event: ErrorEvent) -> bool:
    logger.error("Update handling failed", exc_info=event.exception)
    return True


async def setup_commands() -> None:
    await bot.set_my_commands(CLIENT_COMMANDS, scope=BotCommandScopeDefault())
    for admin_id in settings.admin_id_set:
        try:
            await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id))
        except TelegramBadRequest:
            logger.warning("Admin %s has not opened the bot yet", admin_id)


async def wait_for_database() -> None:
    delay = 1
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            await ping_database()
            return
        except Exception as exc:
            last_error = exc
            logger.warning("Database is not ready, attempt %s/5: %s", attempt + 1, exc)
            await asyncio.sleep(delay)
            delay *= 2
    if last_error is not None:
        raise last_error


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    global bot_username
    await wait_for_database()
    await init_models()
    async with SessionFactory() as session:
        await prepare_database(session)
    me = await bot.get_me()
    bot_username = me.username or ""
    await setup_commands()
    polling_task: asyncio.Task[None] | None = None
    if settings.mode == "webhook":
        # URL: {WEBHOOK_BASE_URL}/webhook/{WEBHOOK_SECRET}
        # drop_pending_updates=False — апдейт, разбудивший Render после сна, не теряется.
        await bot.set_webhook(
            url=settings.webhook_url,
            allowed_updates=dp.resolve_used_update_types(),
            drop_pending_updates=False,
            secret_token=settings.webhook_secret,
        )
        logger.info("Webhook is set to %s", settings.webhook_url)
    else:
        await bot.delete_webhook(drop_pending_updates=False)
        polling_task = asyncio.create_task(
            dp.start_polling(
                bot,
                handle_signals=False,
                close_bot_session=False,
                allowed_updates=dp.resolve_used_update_types(),
            )
        )
        logger.info("Polling started")
    logger.info("Bot is ready, mode=%s", settings.mode)
    try:
        yield
    finally:
        # Webhook не снимаем. На бесплатном тарифе процесс убивают, когда сервис засыпает,
        # и следующий POST от Telegram должен снова попасть на тот же адрес.
        if polling_task is not None:
            with contextlib.suppress(RuntimeError):
                await dp.stop_polling()
            polling_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await polling_task
        await bot.session.close()
        await dispose_engine()


# В webhook-режиме секрет в пути — не светим его в /docs.
_docs = None if settings.mode == "webhook" else "/docs"
app = FastAPI(
    title="Booking bot",
    lifespan=lifespan,
    docs_url=_docs,
    redoc_url=None if settings.mode == "webhook" else "/redoc",
    openapi_url=None if settings.mode == "webhook" else "/openapi.json",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


class ServiceOut(BaseModel):
    id: int
    name: str
    price: int
    duration_minutes: int


class ServicesResponse(BaseModel):
    studio_name: str
    bot_username: str
    today: date
    until: date
    timezone: str
    services: list[ServiceOut]


class BookRequest(BaseModel):
    service_id: int = Field(gt=0)
    date: date
    slot_time: str
    init_data: str = ""


class BookResponse(BaseModel):
    ok: bool
    message: str
    appointment_id: int | None = None


class SlotOut(BaseModel):
    time: str
    end: str


class SlotsResponse(BaseModel):
    service_id: int
    date: date
    slots: list[SlotOut]


class DayOut(BaseModel):
    date: date
    free: int


class CalendarResponse(BaseModel):
    service_id: int
    today: date
    until: date
    start: date
    end: date
    days: list[DayOut]


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "booking-bot", "health": "/health", "webapp": "/webapp/"}


@app.get("/api/services", response_model=ServicesResponse)
async def api_services(response: Response) -> ServicesResponse:
    _no_store(response)
    async with SessionFactory() as session:
        services = await list_services(session)
    return ServicesResponse(
        studio_name=settings.studio_name,
        bot_username=bot_username,
        today=today_local(),
        until=horizon_end(),
        timezone=settings.timezone,
        services=[
            ServiceOut(
                id=item.id,
                name=item.name,
                price=item.price,
                duration_minutes=item.duration_minutes,
            )
            for item in services
        ],
    )


@app.post("/api/book", response_model=BookResponse)
async def api_book(
    body: BookRequest,
    response: Response,
    x_telegram_init_data: str | None = Header(default=None),
) -> BookResponse:
    _no_store(response)
    init_data = body.init_data or x_telegram_init_data or ""
    try:
        user = parse_webapp_user(init_data)
        slot_time = time.fromisoformat(body.slot_time).replace(second=0, microsecond=0)
    except WebAppAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Некорректное время слота") from exc

    async with SessionFactory() as session:
        outcome = await create_booking_and_invoice(
            session,
            bot,
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            service_id=body.service_id,
            day=body.date,
            slot_time=slot_time,
        )
    if not outcome.ok:
        raise HTTPException(status_code=409, detail=outcome.message)
    return BookResponse(ok=True, message=outcome.message, appointment_id=outcome.appointment_id)


@app.get("/api/calendar", response_model=CalendarResponse)
async def api_calendar(service_id: int, start: date, end: date, response: Response) -> CalendarResponse:
    _no_store(response)
    if end < start or (end - start).days > 62:
        raise HTTPException(status_code=400, detail="Некорректный диапазон дат")
    async with SessionFactory() as session:
        service, days = await calendar_availability(session, service_id, start, end)
    if service is None:
        raise HTTPException(status_code=404, detail="Услуга не найдена")
    return CalendarResponse(
        service_id=service.id,
        today=today_local(),
        until=horizon_end(),
        start=start,
        end=end,
        days=[DayOut(date=day, free=free) for day, free in days],
    )


@app.get("/api/slots", response_model=SlotsResponse)
async def api_slots(service_id: int, date: date, response: Response) -> SlotsResponse:
    _no_store(response)
    async with SessionFactory() as session:
        service, slots = await available_starts(session, service_id, date)
    if service is None:
        raise HTTPException(status_code=404, detail="Услуга не найдена")
    return SlotsResponse(
        service_id=service.id,
        date=date,
        slots=[SlotOut(time=format_time(start), end=format_time(end)) for start, end in slots],
    )


def _secret_ok(header: str | None) -> bool:
    if not settings.webhook_secret:
        return True
    return hmac.compare_digest(header or "", settings.webhook_secret)


@app.post(settings.webhook_path)
async def telegram_webhook(request: Request) -> Response:
    if not _secret_ok(request.headers.get("x-telegram-bot-api-secret-token")):
        return Response(status_code=401)
    try:
        payload = await request.json()
        update = Update.model_validate(payload, context={"bot": bot})
    except Exception:
        logger.warning("Rejected webhook payload", exc_info=True)
        return Response(status_code=400)
    try:
        await dp.feed_update(bot, update)
    except Exception:
        logger.exception("Webhook processing failed")
        return Response(status_code=500)
    return Response(status_code=200)


app.mount("/webapp", StaticFiles(directory=ROOT_DIR / "webapp", html=True), name="webapp")


def main() -> None:
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
