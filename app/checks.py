"""Выполнение проверки: опрос источников по очереди и запись результатов в БД."""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import async_sessionmaker

from .captcha import CaptchaBroker
from .models import Check, CheckStatus, SourceResult, utcnow
from .sources import Source, SourceContext, SourceData

log = logging.getLogger(__name__)


class CheckRunner:
    def __init__(self, sessionmaker: async_sessionmaker, sources: list[Source],
                 captcha: CaptchaBroker, captcha_timeout: float = 180):
        self.sessionmaker = sessionmaker
        self.sources = sources
        self.captcha = captcha
        self.captcha_timeout = captcha_timeout

    async def _set_status(self, check_id: int, status: str) -> None:
        async with self.sessionmaker() as db:
            check = await db.get(Check, check_id)
            check.status = status
            await db.commit()

    async def run(self, check_id: int) -> None:
        async with self.sessionmaker() as db:
            check = await db.get(Check, check_id)
            query = check.query
        await self._set_status(check_id, CheckStatus.RUNNING)

        async def ask_captcha(image: bytes) -> str:
            await self._set_status(check_id, CheckStatus.CAPTCHA)
            try:
                return await self.captcha.ask(check_id, image, self.captcha_timeout)
            finally:
                await self._set_status(check_id, CheckStatus.RUNNING)

        ctx = SourceContext(query=query, ask_captcha=ask_captcha)
        for source in self.sources:
            row = SourceResult(check_id=check_id, source=source.name)
            try:
                data = await source.fetch(ctx)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # один упавший источник не должен ронять всю проверку
                log.exception("источник %s, проверка %s", source.name, check_id)
                row.status, row.error = "error", f"{type(e).__name__}: {e}"
            else:
                if data.skipped:
                    continue
                ctx.previous[source.name] = data
                row.status = "found" if data.found else "not_found"
                row.summary = [list(p) for p in data.summary]
                row.data = data.data
            async with self.sessionmaker() as db:
                db.add(row)
                await db.commit()

        async with self.sessionmaker() as db:
            check = await db.get(Check, check_id)
            identity: dict[str, str] = {}
            for data in reversed(list(ctx.previous.values())):  # первые источники важнее
                identity.update(data.identity)
            check.title = identity.get("title")
            check.inn = identity.get("inn")
            check.ogrn = identity.get("ogrn")
            if ctx.previous:
                check.status = CheckStatus.DONE
            else:
                check.status = CheckStatus.ERROR
                check.error = "Ни один источник не ответил"
            check.finished_at = utcnow()
            await db.commit()


def format_status(status: str) -> str:
    return {
        CheckStatus.QUEUED: "В очереди",
        CheckStatus.RUNNING: "Выполняется",
        CheckStatus.CAPTCHA: "Нужна капча",
        CheckStatus.DONE: "Готово",
        CheckStatus.ERROR: "Ошибка",
    }.get(status, status)
