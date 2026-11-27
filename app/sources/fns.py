"""ФНС «Прозрачный бизнес» через модуль pbnalog.

Основная карточка берётся из DaData, а pb.nalog.ru дополняет её тем, чего в
DaData нет (на бесплатном тарифе — учредители и связанные лица), и заменяет её,
если DaData не ответила. Режим задаётся PBNALOG_MODE:
  always   — всегда (по умолчанию);
  fallback — только если DaData не нашла компанию или ответила ошибкой
             (меньше капчи, но на бесплатном тарифе DaData без учредителей);
  off      — не использовать.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os

import httpx

from pbnalog import CaptchaRequired, NotFound, PbNalogClient
from pbnalog.summary import FIELDS, kind_title, pick, summarize

from ..captcha import CaptchaTimeout
from . import AskCaptcha, SourceContext, SourceData

# Клиент общий на все проверки (одна сессия и общий лимит запросов к сайту),
# а капчу нужно показать тому, чья проверка сейчас идёт.
_current_ask: contextvars.ContextVar[AskCaptcha] = contextvars.ContextVar("fns_ask_captcha")


async def _solver(image: bytes) -> str:
    return await _current_ask.get()(image)


MODES = ("always", "fallback", "off")
log = logging.getLogger(__name__)


def _reason(e: Exception) -> str:
    if isinstance(e, CaptchaTimeout):
        return "капчу не ввели вовремя"
    if isinstance(e, CaptchaRequired):
        return "сайт просит капчу"
    if isinstance(e, (TimeoutError, httpx.TimeoutException)):
        return "нет ответа"
    if isinstance(e, httpx.HTTPError):
        return "сайт недоступен"
    return "ошибка сайта"


class FnsSource:
    name = "fns"
    title = "ФНС: Прозрачный бизнес"

    def __init__(self, client: PbNalogClient | None = None, mode: str | None = None):
        self.client = client or PbNalogClient()
        self.client.solver = _solver
        self.mode = (mode or os.environ.get("PBNALOG_MODE") or "always").strip().lower()
        if self.mode not in MODES:
            raise ValueError(f"PBNALOG_MODE: ожидается одно из {', '.join(MODES)}, а не {self.mode!r}")

    async def fetch(self, ctx: SourceContext) -> SourceData:
        dadata = ctx.previous.get("dadata")
        primary = dadata is not None and dadata.found
        if self.mode == "off" or (self.mode == "fallback" and primary):
            return SourceData(found=False, skipped=True)
        _current_ask.set(ctx.ask_captcha)
        try:
            result = await self.client.lookup(ctx.query)
        except NotFound:
            return SourceData(found=False)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if not primary:
                raise  # карточки нет ни откуда — это настоящая ошибка
            # DaData уже ответила: отказ сайта ФНС (капча без ответа, блокировка,
            # таймаут) не ошибка проверки, а пометка, что дополнения нет.
            log.warning("pb.nalog.ru не ответил, карточка из DaData: %s: %s", type(e).__name__, e)
            reason = _reason(e)
            return SourceData(found=True, summary=[
                ("Внимание", f"pb.nalog.ru не ответил ({reason}). Карточка показана по данным DaData"),
            ], data={"unavailable": True, "error": f"{type(e).__name__}: {e}"})
        merged = {**result.get("card", {}), **result["hit"]}
        identity = {"kind": result["kind"]}
        for key, (_, fields) in zip(("title", "inn", "ogrn"), FIELDS[:3]):
            value = pick(merged, fields)
            if value is not None:
                identity[key] = str(value)
        summary = [("Тип", kind_title(result["kind"])), *summarize(result)]
        if primary:  # то же самое уже показано в карточке DaData
            shown = {label for label, _ in dadata.summary}
            summary = [(k, v) for k, v in summary if k not in shown]
        return SourceData(found=True, summary=summary, data=result, identity=identity)

    async def aclose(self) -> None:
        await self.client.aclose()
