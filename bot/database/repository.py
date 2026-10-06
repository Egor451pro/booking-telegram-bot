"""Запросы и транзакции. Наружу уходят только простые структуры, не ORM-объекты."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.database.models import Appointment, AppointmentStatus, Service, TimeSlot, User
from bot.schedule import (
    horizon_end,
    starts_in_future,
    today_local,
    visit_end,
    window_for,
    work_times,
)

logger = logging.getLogger(__name__)

ACTIVE = (AppointmentStatus.pending, AppointmentStatus.confirmed)
SEED_SERVICES: tuple[tuple[str, int, int], ...] = (
    ("Стрижка", 1500, 60),
    ("Окрашивание", 3500, 120),
    ("Укладка", 1200, 45),
    ("Мужская стрижка", 1000, 30),
)


@dataclass(frozen=True)
class ServiceInfo:
    id: int
    name: str
    price: int
    duration_minutes: int


@dataclass(frozen=True)
class BookingResult:
    ok: bool
    code: str
    appointment_id: int | None = None
    service_name: str = ""
    price: int = 0
    duration_minutes: int = 0
    day: date | None = None
    start: time | None = None
    end: time | None = None


@dataclass(frozen=True)
class PaymentView:
    state: str
    service_name: str = ""
    price: int = 0
    duration_minutes: int = 0
    day: date | None = None
    start: time | None = None
    end: time | None = None
    charge_id: str = ""


@dataclass(frozen=True)
class AppointmentCard:
    id: int
    user_id: int
    full_name: str
    username: str | None
    service_name: str
    price: int
    duration_minutes: int
    day: date
    start: time
    end: time
    status: AppointmentStatus
    charge_id: str | None


@dataclass(frozen=True)
class SlotAdminView:
    id: int
    start: time
    state: str
    appointment_id: int | None = None


@dataclass(frozen=True)
class DayAdminView:
    day: date
    free: int
    blocked: int
    booked: int


def _service_info(service: Service) -> ServiceInfo:
    return ServiceInfo(
        id=service.id,
        name=service.name,
        price=service.price,
        duration_minutes=service.duration_minutes,
    )


def _user_display_name(user: User) -> str:
    parts = [part for part in (user.first_name, user.last_name) if part]
    return " ".join(parts) or "Гость"


def _card(appointment: Appointment, slot: TimeSlot, service: Service, user: User) -> AppointmentCard:
    return AppointmentCard(
        id=appointment.id,
        user_id=user.id,
        full_name=_user_display_name(user),
        username=user.username,
        service_name=service.name,
        price=service.price,
        duration_minutes=service.duration_minutes,
        day=slot.date,
        start=slot.time,
        end=visit_end(slot.time, service.duration_minutes),
        status=appointment.status,
        charge_id=appointment.payment_charge_id,
    )


async def _lock_appointment(session: AsyncSession, appointment_id: int) -> Appointment | None:
    statement = (
        select(Appointment)
        .where(Appointment.id == appointment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def _lock_slots(session: AsyncSession, day: date, times: list[time]) -> list[TimeSlot]:
    if not times:
        return []
    statement = (
        select(TimeSlot)
        .where(TimeSlot.date == day, TimeSlot.time.in_(times))
        .order_by(TimeSlot.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return list((await session.execute(statement)).scalars().all())


async def covered_times(
    session: AsyncSession,
    day: date,
    *,
    exclude_ids: set[int] | None = None,
) -> dict[time, int]:
    statement = (
        select(Appointment, TimeSlot, Service)
        .join(TimeSlot, TimeSlot.id == Appointment.slot_id)
        .join(Service, Service.id == Appointment.service_id)
        .where(TimeSlot.date == day, Appointment.status.in_(ACTIVE))
    )
    if exclude_ids:
        statement = statement.where(Appointment.id.notin_(list(exclude_ids)))
    covered: dict[time, int] = {}
    for appointment, slot, service in (await session.execute(statement)).all():
        window = window_for(slot.time, service.duration_minutes) or [slot.time]
        for moment in window:
            covered.setdefault(moment, appointment.id)
    return covered


async def release_window(session: AsyncSession, appointment: Appointment) -> None:
    service = await session.get(Service, appointment.service_id)
    start_slot = await session.get(TimeSlot, appointment.slot_id)
    if service is None or start_slot is None:
        return
    window = window_for(start_slot.time, service.duration_minutes) or [start_slot.time]
    protected = await covered_times(session, start_slot.date, exclude_ids={appointment.id})
    for slot in await _lock_slots(session, start_slot.date, window):
        if slot.time in protected:
            continue
        slot.is_booked = False


async def release_expired_holds(session: AsyncSession) -> int:
    threshold = datetime.now(timezone.utc) - timedelta(minutes=settings.hold_minutes)
    statement = (
        select(Appointment)
        .where(
            Appointment.status == AppointmentStatus.pending,
            Appointment.created_at < threshold,
        )
        .order_by(Appointment.id)
        .with_for_update()
    )
    rows = list((await session.execute(statement)).scalars().all())
    for appointment in rows:
        await release_window(session, appointment)
        appointment.status = AppointmentStatus.cancelled
    if rows:
        await session.flush()
    return len(rows)


async def ensure_schedule(
    session: AsyncSession,
    *,
    start: date | None = None,
    end: date | None = None,
) -> int:
    today = today_local()
    last = horizon_end()
    start = max(start or today, today)
    end = min(end or last, last)
    if start > end:
        return 0
    existing = set(
        (
            await session.execute(
                select(TimeSlot.date, TimeSlot.time).where(TimeSlot.date >= start, TimeSlot.date <= end)
            )
        ).all()
    )
    added = 0
    day = start
    times = work_times()
    while day <= end:
        for slot_time in times:
            if (day, slot_time) not in existing:
                session.add(TimeSlot(date=day, time=slot_time, is_booked=False))
                added += 1
        day += timedelta(days=1)
    if added:
        await session.flush()
    return added


async def seed_services(session: AsyncSession) -> None:
    count = await session.scalar(select(func.count()).select_from(Service))
    if count:
        return
    for name, price, duration in SEED_SERVICES:
        session.add(Service(name=name, price=price, duration_minutes=duration))
    await session.flush()


async def prepare_database(session: AsyncSession) -> None:
    await seed_services(session)
    added = await ensure_schedule(session)
    released = await release_expired_holds(session)
    await session.commit()
    logger.info("Schedule ready, slots added=%s, expired holds released=%s", added, released)


async def sweep(session: AsyncSession) -> None:
    released = await release_expired_holds(session)
    added = await ensure_schedule(session)
    if released or added:
        await session.commit()


async def upsert_user(
    session: AsyncSession,
    *,
    user_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
    is_active: bool = True,
) -> None:
    user = await session.get(User, user_id)
    if user is None:
        session.add(
            User(
                id=user_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
                is_active=is_active,
            )
        )
    else:
        user.username = username
        user.first_name = first_name
        user.last_name = last_name
        user.is_active = is_active
    await session.commit()


async def list_services(session: AsyncSession) -> list[ServiceInfo]:
    await sweep(session)
    rows = list((await session.scalars(select(Service).order_by(Service.id))).all())
    return [_service_info(row) for row in rows]


async def get_service(session: AsyncSession, service_id: int) -> ServiceInfo | None:
    service = await session.get(Service, service_id)
    if service is None:
        return None
    return _service_info(service)


def _count_starts(slots_by_time: dict[time, TimeSlot], duration: int, day: date) -> int:
    free = 0
    for start in work_times():
        window = window_for(start, duration)
        if window is None or not starts_in_future(day, start):
            continue
        if all(moment in slots_by_time and not slots_by_time[moment].is_booked for moment in window):
            free += 1
    return free


async def calendar_availability(
    session: AsyncSession,
    service_id: int,
    start: date,
    end: date,
) -> tuple[ServiceInfo | None, list[tuple[date, int]]]:
    await sweep(session)
    service = await get_service(session, service_id)
    if service is None:
        return None, []
    start = max(start, today_local())
    end = min(end, horizon_end())
    if start > end:
        return service, []
    slots = list(
        (
            await session.scalars(
                select(TimeSlot)
                .where(TimeSlot.date >= start, TimeSlot.date <= end)
                .order_by(TimeSlot.date, TimeSlot.time)
            )
        ).all()
    )
    grouped: dict[date, dict[time, TimeSlot]] = {}
    for slot in slots:
        grouped.setdefault(slot.date, {})[slot.time] = slot
    days: list[tuple[date, int]] = []
    cursor = start
    while cursor <= end:
        days.append((cursor, _count_starts(grouped.get(cursor, {}), service.duration_minutes, cursor)))
        cursor += timedelta(days=1)
    return service, days


async def available_starts(
    session: AsyncSession,
    service_id: int,
    day: date,
) -> tuple[ServiceInfo | None, list[tuple[time, time]]]:
    await sweep(session)
    service = await get_service(session, service_id)
    if service is None:
        return None, []
    if day < today_local() or day > horizon_end():
        return service, []
    slots = {
        slot.time: slot
        for slot in (
            await session.scalars(select(TimeSlot).where(TimeSlot.date == day).order_by(TimeSlot.time))
        ).all()
    }
    starts: list[tuple[time, time]] = []
    for start in work_times():
        window = window_for(start, service.duration_minutes)
        if window is None or not starts_in_future(day, start):
            continue
        if all(moment in slots and not slots[moment].is_booked for moment in window):
            starts.append((start, visit_end(start, service.duration_minutes)))
    return service, starts


async def _lock_user_pending(session: AsyncSession, user_id: int) -> list[Appointment]:
    statement = (
        select(Appointment)
        .where(Appointment.user_id == user_id, Appointment.status == AppointmentStatus.pending)
        .order_by(Appointment.id)
        .with_for_update()
    )
    return list((await session.execute(statement)).scalars().all())


async def book_slot(
    session: AsyncSession,
    *,
    user_id: int,
    service_id: int,
    day: date,
    slot_time: time,
) -> BookingResult:
    slot_time = slot_time.replace(second=0, microsecond=0)
    try:
        await release_expired_holds(session)
        await ensure_schedule(session)
        own_pending = await _lock_user_pending(session, user_id)
        service = await session.get(Service, service_id)
        if service is None:
            await session.commit()
            return BookingResult(False, "no_service")
        if day < today_local() or day > horizon_end():
            await session.commit()
            return BookingResult(False, "bad_date")
        window = window_for(slot_time, service.duration_minutes)
        if window is None:
            await session.commit()
            return BookingResult(False, "unfit")
        if not starts_in_future(day, slot_time):
            await session.commit()
            return BookingResult(False, "past")

        own_ids = {appointment.id for appointment in own_pending}
        own_times: set[time] = set()
        for appointment in own_pending:
            start_slot = await session.get(TimeSlot, appointment.slot_id)
            own_service = await session.get(Service, appointment.service_id)
            if start_slot is None or own_service is None or start_slot.date != day:
                continue
            own_times.update(window_for(start_slot.time, own_service.duration_minutes) or [start_slot.time])
        others = await covered_times(session, day, exclude_ids=own_ids)
        slots = await _lock_slots(session, day, window)
        by_time = {slot.time: slot for slot in slots}
        if len(slots) != len(window):
            await session.commit()
            return BookingResult(False, "missing_slot")
        for moment in window:
            slot = by_time[moment]
            if moment in others:
                await session.commit()
                return BookingResult(False, "taken")
            if slot.is_booked and moment not in own_times:
                await session.commit()
                return BookingResult(False, "closed")

        for appointment in own_pending:
            await release_window(session, appointment)
            appointment.status = AppointmentStatus.cancelled
        await session.flush()
        fresh = await _lock_slots(session, day, window)
        if len(fresh) != len(window) or any(slot.is_booked for slot in fresh):
            await session.rollback()
            return BookingResult(False, "taken")
        for slot in fresh:
            slot.is_booked = True
        start_slot = next(slot for slot in fresh if slot.time == window[0])
        appointment = Appointment(
            user_id=user_id,
            service_id=service.id,
            slot_id=start_slot.id,
            status=AppointmentStatus.pending,
            created_at=datetime.now(timezone.utc),
        )
        session.add(appointment)
        await session.flush()
        result = BookingResult(
            ok=True,
            code="ok",
            appointment_id=appointment.id,
            service_name=service.name,
            price=service.price,
            duration_minutes=service.duration_minutes,
            day=day,
            start=slot_time,
            end=visit_end(slot_time, service.duration_minutes),
        )
        await session.commit()
        return result
    except IntegrityError:
        logger.info("Booking conflict for user %s on %s %s", user_id, day, slot_time)
        await session.rollback()
        return BookingResult(False, "busy")
    except OperationalError as exc:
        await session.rollback()
        if "deadlock" in str(exc).lower():
            logger.info("Booking deadlock for user %s", user_id)
            return BookingResult(False, "busy")
        raise


def _payment_view(
    state: str,
    service: Service,
    slot: TimeSlot,
    charge_id: str = "",
) -> PaymentView:
    return PaymentView(
        state=state,
        service_name=service.name,
        price=service.price,
        duration_minutes=service.duration_minutes,
        day=slot.date,
        start=slot.time,
        end=visit_end(slot.time, service.duration_minutes),
        charge_id=charge_id,
    )


async def check_pre_checkout(session: AsyncSession, appointment_id: int, user_id: int) -> str | None:
    """Быстрая проверка перед списанием. Не отменяем бронь здесь: на это есть sweep."""
    appointment = await _lock_appointment(session, appointment_id)
    if appointment is None or appointment.user_id != user_id:
        await session.commit()
        return "Запись не найдена. Откройте календарь и выберите время снова."
    if appointment.status == AppointmentStatus.confirmed:
        await session.commit()
        return "Этот счёт уже оплачен."
    if appointment.status != AppointmentStatus.pending:
        await session.commit()
        return "Счёт больше недействителен. Запишитесь снова."
    start_slot = await session.get(TimeSlot, appointment.slot_id)
    if start_slot is None or not start_slot.is_booked:
        appointment.status = AppointmentStatus.cancelled
        await session.commit()
        return "Это время уже свободно ушло. Запишитесь снова."
    # Продлеваем hold, чтобы медленная оплата в Telegram не срывалась на 10-й минуте.
    appointment.created_at = datetime.now(timezone.utc)
    await session.commit()
    return None


async def _mark_window(session: AsyncSession, appointment: Appointment) -> bool:
    service = await session.get(Service, appointment.service_id)
    start_slot = await session.get(TimeSlot, appointment.slot_id)
    if service is None or start_slot is None:
        return False
    window = window_for(start_slot.time, service.duration_minutes) or [start_slot.time]
    slots = await _lock_slots(session, start_slot.date, window)
    if len(slots) != len(window):
        return False
    if any(slot.is_booked for slot in slots):
        return False
    for slot in slots:
        slot.is_booked = True
    return True


async def confirm_payment(
    session: AsyncSession,
    appointment_id: int,
    user_id: int,
    charge_id: str,
) -> PaymentView:
    appointment = await _lock_appointment(session, appointment_id)
    if appointment is None or appointment.user_id != user_id:
        await session.commit()
        return PaymentView("missing", charge_id=charge_id)
    service = await session.get(Service, appointment.service_id)
    slot = await session.get(TimeSlot, appointment.slot_id)
    if service is None or slot is None:
        await session.commit()
        return PaymentView("missing", charge_id=charge_id)
    if appointment.status == AppointmentStatus.confirmed:
        view = _payment_view("already", service, slot, appointment.payment_charge_id or charge_id)
        await session.commit()
        return view
    if appointment.status == AppointmentStatus.cancelled:
        reclaimed = await _mark_window(session, appointment)
        if not reclaimed:
            await session.commit()
            return PaymentView("lost", charge_id=charge_id)
    elif appointment.status != AppointmentStatus.pending:
        await session.commit()
        return PaymentView("rejected", charge_id=charge_id)
    appointment.status = AppointmentStatus.confirmed
    appointment.payment_charge_id = charge_id
    view = _payment_view("confirmed", service, slot, charge_id)
    await session.commit()
    return view


async def get_card(
    session: AsyncSession,
    appointment_id: int,
    *,
    user_id: int | None = None,
) -> AppointmentCard | None:
    await sweep(session)
    statement = (
        select(Appointment, TimeSlot, Service, User)
        .join(TimeSlot, TimeSlot.id == Appointment.slot_id)
        .join(Service, Service.id == Appointment.service_id)
        .join(User, User.id == Appointment.user_id)
        .where(Appointment.id == appointment_id)
    )
    if user_id is not None:
        statement = statement.where(Appointment.user_id == user_id)
    row = (await session.execute(statement)).first()
    if row is None:
        return None
    return _card(*row)


async def list_cards(
    session: AsyncSession,
    *,
    user_id: int | None,
    scope: str,
    offset: int,
    limit: int,
) -> tuple[list[AppointmentCard], int]:
    await sweep(session)
    today = today_local()
    filters = [Appointment.status.in_(ACTIVE)]
    if user_id is not None:
        filters.append(Appointment.user_id == user_id)
        filters.append(TimeSlot.date >= today)
    elif scope == "today":
        filters.append(TimeSlot.date == today)
    else:
        filters.append(TimeSlot.date > today)
        filters.append(TimeSlot.date <= horizon_end())
    join_slot = TimeSlot.id == Appointment.slot_id
    total = await session.scalar(
        select(func.count(Appointment.id)).select_from(Appointment).join(TimeSlot, join_slot).where(*filters)
    )
    rows = (
        await session.execute(
            select(Appointment, TimeSlot, Service, User)
            .join(TimeSlot, join_slot)
            .join(Service, Service.id == Appointment.service_id)
            .join(User, User.id == Appointment.user_id)
            .where(*filters)
            .order_by(TimeSlot.date, TimeSlot.time, Appointment.id)
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return [_card(*row) for row in rows], int(total or 0)


async def admin_counts(session: AsyncSession) -> tuple[int, int]:
    await sweep(session)
    today = today_local()
    last = horizon_end()

    async def _count(*extra: object) -> int:
        value = await session.scalar(
            select(func.count(Appointment.id))
            .select_from(Appointment)
            .join(TimeSlot, TimeSlot.id == Appointment.slot_id)
            .where(Appointment.status.in_(ACTIVE), *extra)
        )
        return int(value or 0)

    return await _count(TimeSlot.date == today), await _count(TimeSlot.date > today, TimeSlot.date <= last)


async def cancel_appointment(
    session: AsyncSession,
    appointment_id: int,
    *,
    actor_id: int,
    as_admin: bool,
) -> str:
    await sweep(session)
    appointment = await _lock_appointment(session, appointment_id)
    if appointment is None:
        await session.commit()
        return "not_found"
    if not as_admin and appointment.user_id != actor_id:
        await session.commit()
        return "forbidden"
    if appointment.status == AppointmentStatus.cancelled:
        await session.commit()
        return "already"
    if appointment.status not in ACTIVE:
        await session.commit()
        return "already"
    start_slot = await session.get(TimeSlot, appointment.slot_id)
    if start_slot is None:
        await session.commit()
        return "not_found"
    if (
        not as_admin
        and appointment.status == AppointmentStatus.confirmed
        and not starts_in_future(start_slot.date, start_slot.time)
    ):
        await session.commit()
        return "past"
    await release_window(session, appointment)
    appointment.status = AppointmentStatus.cancelled
    await session.commit()
    return "ok"


async def admin_days(session: AsyncSession) -> list[DayAdminView]:
    await sweep(session)
    today = today_local()
    last = horizon_end()
    slots = list(
        (
            await session.scalars(
                select(TimeSlot).where(TimeSlot.date >= today, TimeSlot.date <= last).order_by(TimeSlot.date, TimeSlot.time)
            )
        ).all()
    )
    grouped: dict[date, list[TimeSlot]] = {}
    for slot in slots:
        grouped.setdefault(slot.date, []).append(slot)
    rows = (
        await session.execute(
            select(Appointment, TimeSlot, Service)
            .join(TimeSlot, TimeSlot.id == Appointment.slot_id)
            .join(Service, Service.id == Appointment.service_id)
            .where(
                TimeSlot.date >= today,
                TimeSlot.date <= last,
                Appointment.status.in_(ACTIVE),
            )
        )
    ).all()
    coverage: dict[date, set[time]] = {}
    for _appointment, slot, service in rows:
        window = window_for(slot.time, service.duration_minutes) or [slot.time]
        coverage.setdefault(slot.date, set()).update(window)
    days: list[DayAdminView] = []
    cursor = today
    while cursor <= last:
        free = blocked = booked = 0
        taken = coverage.get(cursor, set())
        for slot in grouped.get(cursor, []):
            if slot.time in taken:
                booked += 1
            elif slot.is_booked:
                blocked += 1
            else:
                free += 1
        days.append(DayAdminView(day=cursor, free=free, blocked=blocked, booked=booked))
        cursor += timedelta(days=1)
    return days


async def admin_day_slots(session: AsyncSession, day: date) -> list[SlotAdminView]:
    await sweep(session)
    slots = list(
        (await session.scalars(select(TimeSlot).where(TimeSlot.date == day).order_by(TimeSlot.time))).all()
    )
    covered = await covered_times(session, day)
    views: list[SlotAdminView] = []
    for slot in slots:
        appointment_id = covered.get(slot.time)
        if appointment_id is not None:
            state = "booked"
        elif slot.is_booked:
            state = "blocked"
        else:
            state = "free"
        views.append(
            SlotAdminView(
                id=slot.id,
                start=slot.time,
                state=state,
                appointment_id=appointment_id,
            )
        )
    return views


async def toggle_slot(session: AsyncSession, slot_id: int) -> tuple[str, date | None]:
    await sweep(session)
    statement = select(TimeSlot).where(TimeSlot.id == slot_id).with_for_update().execution_options(populate_existing=True)
    slot = (await session.execute(statement)).scalar_one_or_none()
    if slot is None:
        await session.commit()
        return "missing", None
    covered = await covered_times(session, slot.date)
    if slot.time in covered:
        day = slot.date
        await session.commit()
        return "booked", day
    slot.is_booked = not slot.is_booked
    code = "closed" if slot.is_booked else "opened"
    day = slot.date
    await session.commit()
    return code, day


async def set_day_closed(session: AsyncSession, day: date, *, closed: bool) -> str:
    await sweep(session)
    slots = list(
        (
            await session.scalars(
                select(TimeSlot)
                .where(TimeSlot.date == day)
                .order_by(TimeSlot.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    if not slots:
        await session.commit()
        return "missing"
    covered = await covered_times(session, day)
    if closed:
        for slot in slots:
            slot.is_booked = True
        await session.commit()
        return "closed"
    for slot in slots:
        if slot.time not in covered:
            slot.is_booked = False
    await session.commit()
    return "opened"
