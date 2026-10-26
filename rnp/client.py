"""Асинхронный клиент реестра недобросовестных поставщиков (РНП) в ЕИС.

Официального API у ЕИС для РНП нет, клиент повторяет поиск веб-интерфейса:

    GET https://zakupki.gov.ru/epz/dishonestsupplier/search/results.html?searchString=<ИНН>

и разбирает HTML-выдачу. Каждая запись — блок ``registry-entry__form`` с номером
и ссылкой (``registry-entry__header-mid__number``), законом (44-ФЗ, 223-ФЗ),
парами «подпись — значение» (``registry-entry__body-title`` / ``-value``) и
датами справа (``data-block__title`` / ``data-block__value``).
Названия подписей берутся как есть, поэтому разбор не ломается от новых полей.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, Tag

from pbnalog.validators import is_valid_inn

log = logging.getLogger(__name__)

BASE_URL = "https://zakupki.gov.ru"
SEARCH_PATH = "/epz/dishonestsupplier/search/results.html"

DEFAULT_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


class RnpError(Exception):
    pass


@dataclass
class RnpEntry:
    number: str                    # реестровый номер записи
    url: str                       # карточка записи в ЕИС
    law: str = ""                  # 44-ФЗ, 223-ФЗ, ...
    status: str = ""               # подпись статуса, если сайт её показывает
    name: str = ""
    inn: str = ""
    fields: dict[str, str] = field(default_factory=dict)   # все пары «подпись — значение»
    dates: dict[str, str] = field(default_factory=dict)    # Включено, Обновлено, ...

    @property
    def active(self) -> bool:
        """Запись действует, пока поставщик не исключён из реестра."""
        if "исключ" in self.status.lower():
            return False
        for key, value in {**self.fields, **self.dates}.items():
            k = key.lower()
            if value and "исключ" in k and "планир" not in k:
                return False
        return True

    def to_dict(self) -> dict:
        return {**asdict(self), "active": self.active}


def _text(node: Tag | None) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def _pairs(block: Tag, prefix: str) -> dict[str, str]:
    """Собирает пары <prefix>title / <prefix>value в порядке появления."""
    out: dict[str, str] = {}
    title = None
    for node in block.find_all(class_=re.compile(rf"^{re.escape(prefix)}(title|value)$")):
        if f"{prefix}title" in node["class"]:
            title = _text(node)
        elif title is not None:
            out.setdefault(title, _text(node))
            title = None
    return out


def _find(fields: dict[str, str], *words: str) -> str:
    for key, value in fields.items():
        if all(w in key.lower() for w in words):
            return value
    return ""


def parse_results(html: str) -> list[RnpEntry]:
    soup = BeautifulSoup(html, "html.parser")
    entries = []
    for block in soup.select("div.registry-entry__form"):
        num = block.select_one(".registry-entry__header-mid__number")
        link = num.find("a") if num else None
        fields = _pairs(block, "registry-entry__body-")
        entries.append(RnpEntry(
            number=re.sub(r"^№\s*", "", _text(num)),
            url=urljoin(BASE_URL, link["href"]) if link and link.get("href") else "",
            law=_text(block.select_one(".registry-entry__header-top__title")),
            status=_text(block.select_one(".registry-entry__header-mid__title")),
            name=_find(fields, "наименование") or _find(fields, "фио"),
            inn=re.sub(r"\D", "", _find(fields, "инн")),
            fields=fields,
            dates=_pairs(block, "data-block__"),
        ))
    return entries


class RnpClient:
    def __init__(self, *, min_interval: float = 1.0, timeout: float = 30.0,
                 http: httpx.AsyncClient | None = None):
        self.min_interval = min_interval
        self._http = http or httpx.AsyncClient(
            base_url=BASE_URL, headers=DEFAULT_HEADERS, timeout=timeout, follow_redirects=True)
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    async def __aenter__(self) -> "RnpClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _throttle(self) -> None:
        async with self._lock:
            wait = self._last_request + self.min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()

    async def search(self, query: str, page: int = 1, page_size: int = 50) -> list[RnpEntry]:
        """Поиск по ИНН, названию или номеру записи (как строка поиска на сайте)."""
        await self._throttle()
        params = {"searchString": query, "morphology": "on", "pageNumber": str(page),
                  "recordsPerPage": f"_{page_size}", "sortBy": "UPDATE_DATE",
                  "sortDirection": "false"}
        resp = await self._http.get(SEARCH_PATH, params=params)
        log.debug("GET %s -> %s %d bytes", resp.url, resp.status_code, len(resp.content))
        if resp.status_code != 200:
            raise RnpError(f"zakupki.gov.ru: HTTP {resp.status_code}")
        return parse_results(resp.text)

    async def check(self, inn: str) -> list[RnpEntry]:
        """Записи РНП по ИНН. Пустой список — в реестре не найден."""
        q = re.sub(r"\D", "", inn)
        if not is_valid_inn(q):
            raise ValueError(f"{inn!r} не похоже на корректный ИНН")
        # Полнотекстовый поиск может зацепить чужие записи (например, ИНН учредителя),
        # поэтому оставляем только записи с этим ИНН поставщика.
        return [e for e in await self.search(q) if e.inn == q]
