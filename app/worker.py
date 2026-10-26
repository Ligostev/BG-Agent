"""Воркер очереди проверок: arq app.worker.WorkerSettings"""

from __future__ import annotations

from arq.connections import RedisSettings

from .captcha import RedisCaptchaBroker
from .checks import CheckRunner
from .config import settings
from .db import create_tables, make_engine, make_sessionmaker
from .main import default_sources


async def startup(ctx: dict) -> None:
    ctx["engine"] = make_engine(settings.database_url)
    await create_tables(ctx["engine"])
    ctx["sources"] = default_sources()
    ctx["runner"] = CheckRunner(make_sessionmaker(ctx["engine"]), ctx["sources"],
                                RedisCaptchaBroker(ctx["redis"]), settings.captcha_timeout)


async def shutdown(ctx: dict) -> None:
    for source in ctx["sources"]:
        if hasattr(source, "aclose"):
            await source.aclose()
    await ctx["engine"].dispose()


async def run_check(ctx: dict, check_id: int) -> None:
    await ctx["runner"].run(check_id)


class WorkerSettings:
    functions = [run_check]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url or "redis://localhost:6379")
    # Капча ждёт пользователя до CAPTCHA_TIMEOUT, поэтому задаче нужен запас по времени.
    job_timeout = 900
    max_tries = 1
    # Все источники делят один клиент с лимитом запросов, много параллельных задач не нужно.
    max_jobs = 4
