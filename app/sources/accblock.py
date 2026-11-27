"""Блокировки счетов: решения ФНС о приостановлении операций и красная зона ЦБ (ЗСК).

Если сервис ФНС не ответил (капчу не ввели, сайт недоступен, ответ не разобран),
источник не падает: в отчёте пометка «не проверено» и ссылка для ручной
проверки, а остальные источники показываются как обычно.
"""

from __future__ import annotations

import asyncio
import logging
import re

import httpx

from accblock import (AccountBlockReport, CaptchaRequired, FnsBlocksClient, FnsFormatError,
                      FnsResult, ZskClient, ZskResult)

from ..captcha import CaptchaTimeout
from . import SourceContext, SourceData
from .parties import subject

log = logging.getLogger(__name__)


def _reason(e: Exception) -> str:
    if isinstance(e, CaptchaTimeout):
        return "капчу не ввели вовремя"
    if isinstance(e, CaptchaRequired):
        return "капча не пройдена"
    if isinstance(e, FnsFormatError):
        return "ответ сервиса не удалось разобрать"
    if isinstance(e, (TimeoutError, httpx.TimeoutException)):
        return "нет ответа"
    if isinstance(e, httpx.HTTPError):
        return "сайт недоступен"
    return "ошибка сервиса"


class AccountBlocksSource:
    name = "accblock"
    title = "Блокировки счетов (ФНС) и риск ЦБ"

    def __init__(self, fns: FnsBlocksClient | None = None, zsk: ZskClient | None = None):
        self.fns = fns or FnsBlocksClient()
        self.zsk = zsk or ZskClient()

    async def fetch(self, ctx: SourceContext) -> SourceData:
        query = re.sub(r"\D", "", ctx.query)
        inn = subject(ctx).get("inn") or (query if len(query) in (10, 12) else None)
        if not inn:
            report = AccountBlockReport(None, FnsResult(False, error="нет ИНН"),
                                        ZskResult(False, error="нет ИНН"))
            return SourceData(found=False, summary=report.summary(), data=report.to_dict())

        try:
            fns = FnsResult(True, await self.fns.decisions(inn, solver=ctx.ask_captcha),
                            bik=self.fns.bik)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("bi.do, ИНН %s: %s: %s", inn, type(e).__name__, e)
            fns = FnsResult(False, error=_reason(e), bik=self.fns.bik)

        if self.zsk.configured:
            try:
                high, text = await self.zsk.check(inn)
                zsk = ZskResult(True, high, text)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("ЗСК ЦБ, ИНН %s: %s: %s", inn, type(e).__name__, e)
                zsk = ZskResult(False, error=str(e) if str(e) else _reason(e))
        else:
            zsk = ZskResult(False, error="автоматический запрос не настроен")

        report = AccountBlockReport(inn, fns, zsk)
        return SourceData(found=True, summary=report.summary(), data=report.to_dict())

    async def aclose(self) -> None:
        await self.fns.aclose()
        await self.zsk.aclose()
