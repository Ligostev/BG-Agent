"""Запуск: uvicorn app.main:app

Без REDIS_URL проверки выполняются в процессе веб-сервера. С REDIS_URL они
уходят в очередь arq, и их выполняет отдельный воркер (app.worker).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from . import api, web
from .captcha import CaptchaBroker, MemoryCaptchaBroker, RedisCaptchaBroker
from .checks import CheckRunner
from .config import Settings, settings as default_settings
from .db import create_tables, make_engine, make_sessionmaker
from .deps import LoginRequired
from .sources import Source


def default_sources() -> list[Source]:
    from .sources.fns import FnsSource
    return [FnsSource()]


def create_app(settings: Settings | None = None, sources: list[Source] | None = None,
               captcha: CaptchaBroker | None = None) -> FastAPI:
    settings = settings or default_settings

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = make_engine(settings.database_url)
        await create_tables(engine)
        app.state.sessionmaker = make_sessionmaker(engine)
        app.state.sources = sources if sources is not None else default_sources()
        tasks: set[asyncio.Task] = set()
        pool = None
        if settings.redis_url:
            from arq import create_pool
            from arq.connections import RedisSettings
            pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
            app.state.captcha = captcha or RedisCaptchaBroker(pool)

            async def enqueue(check_id: int) -> None:
                await pool.enqueue_job("run_check", check_id)
        else:
            app.state.captcha = captcha or MemoryCaptchaBroker()
            runner = CheckRunner(app.state.sessionmaker, app.state.sources,
                                 app.state.captcha, settings.captcha_timeout)

            async def enqueue(check_id: int) -> None:
                task = asyncio.create_task(runner.run(check_id))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        app.state.enqueue = enqueue
        app.state.tasks = tasks
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for source in app.state.sources:
                if hasattr(source, "aclose"):
                    await source.aclose()
            if pool is not None:
                await pool.aclose()
            await engine.dispose()

    app = FastAPI(title="BG-Agent: проверка контрагентов", lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key,
                       session_cookie="bg_session", max_age=30 * 24 * 3600,
                       same_site="lax", https_only=settings.cookie_secure)
    app.include_router(api.router)
    app.include_router(web.router)

    @app.exception_handler(LoginRequired)
    async def to_login(request: Request, exc: LoginRequired):
        return RedirectResponse("/login", status_code=303)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"ok": True}

    return app


app = create_app()
