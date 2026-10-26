"""ФНС «Прозрачный бизнес» через модуль pbnalog."""

from __future__ import annotations

import contextvars

from pbnalog import NotFound, PbNalogClient
from pbnalog.summary import FIELDS, kind_title, pick, summarize

from . import AskCaptcha, SourceContext, SourceData

# Клиент общий на все проверки (одна сессия и общий лимит запросов к сайту),
# а капчу нужно показать тому, чья проверка сейчас идёт.
_current_ask: contextvars.ContextVar[AskCaptcha] = contextvars.ContextVar("fns_ask_captcha")


async def _solver(image: bytes) -> str:
    return await _current_ask.get()(image)


class FnsSource:
    name = "fns"
    title = "ФНС: Прозрачный бизнес"

    def __init__(self, client: PbNalogClient | None = None):
        self.client = client or PbNalogClient()
        self.client.solver = _solver

    async def fetch(self, ctx: SourceContext) -> SourceData:
        _current_ask.set(ctx.ask_captcha)
        try:
            result = await self.client.lookup(ctx.query)
        except NotFound:
            return SourceData(found=False)
        merged = {**result.get("card", {}), **result["hit"]}
        identity = {"kind": result["kind"]}
        for key, (_, fields) in zip(("title", "inn", "ogrn"), FIELDS[:3]):
            value = pick(merged, fields)
            if value is not None:
                identity[key] = str(value)
        summary = [("Тип", kind_title(result["kind"])), *summarize(result)]
        return SourceData(found=True, summary=summary, data=result, identity=identity)

    async def aclose(self) -> None:
        await self.client.aclose()
