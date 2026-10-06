"""Чистые функции сетки расписания. База здесь не используется."""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from bot.config import settings

_ZONE = ZoneInfo(settings.timezone)


def now_local() -> datetime:
    return datetime.now(_ZONE)


def today_local() -> date:
    return now_local().date()


def horizon_end() -> date:
    return today_local() + timedelta(days=settings.schedule_days - 1)


def parse_hhmm(value: str) -> time:
    hours_text, minutes_text = value.split(":", maxsplit=1)
    return time(int(hours_text), int(minutes_text))


def format_hhmm(value: time) -> str:
    return value.strftime("%H:%M")


def work_times() -> list[time]:
    start = datetime.combine(date.min, parse_hhmm(settings.work_start))
    end = datetime.combine(date.min, parse_hhmm(settings.work_end))
    step = timedelta(minutes=settings.slot_step_minutes)
    times: list[time] = []
    cursor = start
    while cursor < end:
        times.append(cursor.time())
        cursor += step
    return times


def slots_needed(duration_minutes: int) -> int:
    return max(1, math.ceil(duration_minutes / settings.slot_step_minutes))


def window_for(start: time, duration_minutes: int) -> list[time] | None:
    """Слоты сетки, которые занимает визит. None — визит не помещается в рабочий день."""
    times = work_times()
    try:
        index = times.index(start)
    except ValueError:
        return None
    count = slots_needed(duration_minutes)
    window = times[index : index + count]
    if len(window) < count:
        return None
    return window


def visit_end(start: time, duration_minutes: int) -> time:
    return (datetime.combine(date.min, start) + timedelta(minutes=duration_minutes)).time()


def starts_in_future(day: date, start: time, *, moment: datetime | None = None) -> bool:
    moment = moment or now_local()
    slot_at = datetime.combine(day, start, tzinfo=_ZONE)
    return slot_at > moment
