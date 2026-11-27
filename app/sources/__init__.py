"""Источники данных о контрагенте.

Каждый источник получает ИНН/ОГРН и результаты уже отработавших источников
(например, РНП и ФССП берут название и регион из карточки ФНС) и возвращает
SourceData. Новый источник достаточно реализовать по протоколу Source и
добавить в список, который передаётся в CheckRunner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol

# Решатель капчи в рамках текущей проверки: картинка -> ответ пользователя.
AskCaptcha = Callable[[bytes], Awaitable[str]]


@dataclass
class SourceData:
    found: bool
    summary: list[tuple[str, str]] = field(default_factory=list)
    data: dict | None = None
    # Поля, которые источник знает о контрагенте точно: title, inn, ogrn, kind.
    identity: dict[str, str] = field(default_factory=dict)
    # Источник решил не работать для этой проверки (например, pb.nalog.ru в режиме
    # «только запасной», когда DaData уже ответила): в отчёт он не попадает.
    skipped: bool = False


@dataclass
class SourceContext:
    query: str
    ask_captcha: AskCaptcha
    previous: dict[str, SourceData] = field(default_factory=dict)


class Source(Protocol):
    name: str      # ключ в БД: fns, rnp, fssp
    title: str     # подпись в кабинете и боте

    async def fetch(self, ctx: SourceContext) -> SourceData: ...
