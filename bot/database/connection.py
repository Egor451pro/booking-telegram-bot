"""Асинхронный движок SQLAlchemy. Пул маленький специально для бесплатного Supabase."""

from __future__ import annotations

import ssl

import certifi
from sqlalchemy import text
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.config import settings


def _ssl_arg() -> ssl.SSLContext | None:
    probe = make_url(settings.database_url)
    query = dict(probe.query)
    sslmode = str(query.get("sslmode", "") or "").lower()
    ssl_flag = str(query.get("ssl", "") or "").lower()
    want_ssl = settings.db_ssl or sslmode in {
        "require",
        "verify-ca",
        "verify-full",
        "prefer",
        "true",
        "1",
    } or ssl_flag in {"require", "true", "1"}
    if not want_ssl:
        return None

    context = ssl.create_default_context(cafile=certifi.where())
    verify = settings.db_ssl_verify
    if sslmode in {"require", "prefer", "allow"}:
        verify = False
    elif sslmode in {"verify-ca", "verify-full"}:
        verify = True
    if not verify:
        # Шифрование остаётся, проверка цепочки отключается.
        # Нужно на Windows, если антивирус подменяет сертификат.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


_url = make_url(settings.database_url)
_query = dict(_url.query)
_query.pop("sslmode", None)
_query.pop("ssl", None)
_url = _url.set(query=_query)

_connect_args: dict[str, object] = {
    # Supabase pooler (PgBouncer) не держит prepared statements между транзакциями.
    "statement_cache_size": 0,
}
_ssl = _ssl_arg()
if _ssl is not None:
    _connect_args["ssl"] = _ssl

engine = create_async_engine(
    _url,
    pool_pre_ping=True,
    pool_size=2,
    max_overflow=2,
    pool_recycle=300,
    connect_args=_connect_args,
)

# expire_on_commit=True: после commit кэш сессии не должен переживать чужие брони.
SessionFactory = async_sessionmaker(engine, expire_on_commit=True, class_=AsyncSession)


async def ping_database() -> None:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def init_models() -> None:
    from bot.database.models import Base

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def dispose_engine() -> None:
    await engine.dispose()
