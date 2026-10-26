"""Подключение к БД. В проде PostgreSQL, в тестах и локально SQLite."""

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from .models import Base


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_tables(engine: AsyncEngine) -> None:
    # Пока схема создаётся при старте; миграции (Alembic) добавим, когда появятся живые данные.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
