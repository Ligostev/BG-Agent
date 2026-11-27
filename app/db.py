"""Подключение к БД. В проде PostgreSQL, в тестах и локально SQLite."""

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from .models import Base


def _lower(value):
    return value.lower() if isinstance(value, str) else value


def make_engine(url: str) -> AsyncEngine:
    engine = create_async_engine(url)
    if engine.dialect.name == "sqlite":
        # Встроенный lower() в SQLite не знает кириллицу, из-за чего поиск без учёта регистра
        # (ilike) не находил «Сбербанк» по «сбер». Подменяем его питоновским str.lower.
        @event.listens_for(engine.sync_engine, "connect")
        def _unicode_lower(dbapi_conn, _record):
            dbapi_conn.create_function("lower", 1, _lower, deterministic=True)
    return engine


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_tables(engine: AsyncEngine) -> None:
    # Пока схема создаётся при старте; миграции (Alembic) добавим, когда появятся живые данные.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)


# Колонки, добавленные в уже существующие таблицы: create_all их не создаёт.
_NEW_COLUMNS = {
    "users": {"role": "VARCHAR(16) NOT NULL DEFAULT 'client'", "telegram_code": "VARCHAR(32)"},
    "clients": {"manager_id": "INTEGER REFERENCES users(id)"},
    "deals": {"expires_at": "DATE"},
}


def _add_missing_columns(conn) -> None:
    from sqlalchemy import inspect, text
    insp = inspect(conn)
    for table, columns in _NEW_COLUMNS.items():
        have = {c["name"] for c in insp.get_columns(table)}
        for name, ddl in columns.items():
            if name not in have:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
