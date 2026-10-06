"""Модели записи. Связи не подгружаются автоматически: в async это ломает хэндлеры."""

from __future__ import annotations

import enum
from datetime import date, datetime, time

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class AppointmentStatus(str, enum.Enum):
    pending = "pending"
    confirmed = "confirmed"
    cancelled = "cancelled"


_STATUS = Enum(
    AppointmentStatus,
    name="appointment_status",
    values_callable=lambda cls: [item.value for item in cls],
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    def __repr__(self) -> str:
        return f"User(id={self.id}, username={self.username!r})"


class Service(Base):
    __tablename__ = "services"
    __table_args__ = (
        CheckConstraint("price > 0", name="ck_service_price_positive"),
        CheckConstraint("duration_minutes > 0", name="ck_service_duration_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)

    def __repr__(self) -> str:
        return f"Service(id={self.id}, name={self.name!r})"


class TimeSlot(Base):
    __tablename__ = "time_slots"
    __table_args__ = (
        UniqueConstraint("date", "time", name="uq_slot_date_time"),
        Index("ix_time_slots_date", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    time: Mapped[time] = mapped_column(Time, nullable=False)
    is_booked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    def __repr__(self) -> str:
        return f"TimeSlot(id={self.id}, date={self.date}, time={self.time}, booked={self.is_booked})"


class Appointment(Base):
    __tablename__ = "appointments"
    __table_args__ = (
        Index("ix_appointments_user", "user_id"),
        Index("ix_appointments_status_created", "status", "created_at"),
        # Один активный визит на стартовый слот. Отменённые строки этому индексу не мешают.
        Index(
            "uq_slot_active_appointment",
            "slot_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'confirmed')"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    service_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("services.id", ondelete="RESTRICT"), nullable=False
    )
    slot_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("time_slots.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[AppointmentStatus] = mapped_column(
        _STATUS, nullable=False, default=AppointmentStatus.pending
    )
    payment_charge_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # Нужен, чтобы снять бронь, если клиент открыл счёт и не оплатил его.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    def __repr__(self) -> str:
        return f"Appointment(id={self.id}, status={self.status})"
