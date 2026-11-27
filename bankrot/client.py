"""Клиент публичного поиска ЕФРСБ (bankrot.fedresurs.ru) и Федресурса (fedresurs.ru).

Официальный API ЕФРСБ (services.fedresurs.ru) платный и выдаётся по договору,
поэтому клиент повторяет запросы, которые делают сами сайты при поиске:

    GET bankrot.fedresurs.ru/backend/cmpbankrupts?searchString=<ИНН>   банкротства компаний
    GET bankrot.fedresurs.ru/backend/prsnbankrupts?searchString=<ИНН>  банкротства физлиц и ИП
    GET fedresurs.ru/backend/companies?searchString=<ИНН>              карточка компании (guid)
    GET fedresurs.ru/backend/companies/<guid>/publications             сообщения о компании

Капчи у этих запросов нет, но без заголовка Referer сайт отвечает 403.
Формат ответов не сверен с живым сайтом (облако Claude туда не пускают),
поэтому поля разбираются с запасными вариантами.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

BANKROT_URL = "https://bankrot.fedresurs.ru"
FEDRESURS_URL = "https://fedresurs.ru"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

# Статусы дела, после которых компания или человек уже не в процедуре.
_FINISHED = re.compile(r"заверш|прекращ|отказ|отмен|оставлен[оа]? без рассмотр", re.I)
# Сообщение кредитора о намерении подать на банкротство (п. 2 ст. 7 127-ФЗ).
_INTENTION = re.compile(r"намерени", re.I)


class FedresursError(Exception):
    pass


@dataclass
class Bankruptcy:
    """Запись о банкротстве из ЕФРСБ."""
    kind: str                    # company | person
    name: str
    inn: str | None
    case_number: str | None      # номер дела в арбитражном суде, например А40-12345/2024
    status: str | None           # стадия: наблюдение, конкурсное производство, реализация имущества...
    active: bool                 # процедура идёт (не завершена и не прекращена)
    guid: str | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def url(self) -> str | None:
        if not self.guid:
            return None
        return f"{FEDRESURS_URL}/{'company' if self.kind == 'company' else 'person'}/{self.guid}"

    def describe(self) -> str:
        parts = [self.status or ("процедура идёт" if self.active else "процедура завершена")]
        if self.case_number:
            parts.append(f"дело {self.case_number}")
        return ", ".join(parts)


@dataclass
class Publication:
    type: str
    date: str | None
    number: str | None = None
    guid: str | None = None


def _first(data: dict, *keys: str) -> Any:
    for k in keys:
        v = data.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def _rows(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        rows = _first(payload, "pageData", "data", "items", "result")
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
    return []


def _person_name(rec: dict) -> str:
    name = _first(rec, "fio", "name", "fullName")
    if name:
        return str(name)
    parts = [rec.get(k) for k in ("lastName", "firstName", "middleName")]
    return " ".join(str(p) for p in parts if p) or "?"


def parse_bankruptcy(rec: dict, kind: str) -> Bankruptcy:
    case = _first(rec, "lastLegalCase", "legalCase") or {}
    if isinstance(case, list):
        case = case[0] if case else {}
    status = case.get("status") if isinstance(case, dict) else None
    if isinstance(status, dict):
        status = _first(status, "description", "name", "title")
    status = status or _first(rec, "status", "statusName", "procedureName")
    number = _first(case, "number", "caseNumber") if isinstance(case, dict) else None
    number = number or _first(rec, "caseNumber", "legalCaseNumber")
    active = rec.get("isActiveLegalCase")
    if active is None and isinstance(case, dict):
        active = case.get("isActive")
    if active is None:
        active = not (status and _FINISHED.search(str(status)))
    name = _first(rec, "name", "shortName", "fullName") if kind == "company" else _person_name(rec)
    inn = _first(rec, "inn", "INN")
    return Bankruptcy(kind=kind, name=str(name or "?"), inn=str(inn) if inn else None,
                      case_number=str(number) if number else None,
                      status=str(status) if status else None, active=bool(active),
                      guid=_first(rec, "guid", "id"), raw=rec)


def _matches(rec: dict, ident: str) -> bool:
    ids = {str(rec.get(k) or "") for k in ("inn", "INN", "ogrn", "ogrnip", "OGRN", "snils")}
    return ident in ids


class FedresursClient:
    def __init__(self, http: httpx.AsyncClient | None = None, min_interval: float = 1.0,
                 timeout: float = 20, retries: int = 2):
        self.http = http or httpx.AsyncClient(headers=HEADERS, timeout=timeout,
                                              follow_redirects=True)
        self.min_interval = min_interval
        self.retries = retries
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def __aenter__(self) -> "FedresursClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _get(self, base: str, path: str, params: dict | None = None) -> Any:
        headers = {"Referer": base + "/"}
        for attempt in range(self.retries + 1):
            async with self._lock:  # не чаще одного запроса в min_interval на все проверки
                wait = self._last + self.min_interval - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    r = await self.http.get(base + path, params=params, headers=headers)
                finally:
                    self._last = time.monotonic()
            if r.status_code == 404:
                return None
            if r.status_code in (429, 500, 502, 503, 504) and attempt < self.retries:
                log.warning("%s%s: %s, повтор", base, path, r.status_code)
                await asyncio.sleep(self.min_interval * (attempt + 1) * 2)
                continue
            if r.status_code != 200:
                raise FedresursError(f"{base}{path}: HTTP {r.status_code}")
            try:
                return r.json()
            except ValueError as e:
                raise FedresursError(f"{base}{path}: ответ не JSON") from e
        raise FedresursError(f"{base}{path}: сайт не ответил")

    async def search_company_bankrupts(self, query: str, limit: int = 15) -> list[Bankruptcy]:
        """Банкротства юрлиц по ИНН, ОГРН или названию."""
        data = await self._get(BANKROT_URL, "/backend/cmpbankrupts",
                               {"searchString": query, "isActiveLegalCase": "null",
                                "limit": limit, "offset": 0})
        return [parse_bankruptcy(r, "company") for r in _rows(data)]

    async def search_person_bankrupts(self, query: str, limit: int = 15) -> list[Bankruptcy]:
        """Банкротства физлиц и ИП по ИНН, СНИЛС, ОГРНИП или ФИО."""
        data = await self._get(BANKROT_URL, "/backend/prsnbankrupts",
                               {"searchString": query, "isActiveLegalCase": "null",
                                "limit": limit, "offset": 0})
        return [parse_bankruptcy(r, "person") for r in _rows(data)]

    async def bankruptcies(self, ident: str) -> list[Bankruptcy]:
        """Точные совпадения по ИНН/ОГРН: 10 и 13 цифр это компания, 12 и 15 человек."""
        ident = re.sub(r"\D", "", ident)
        if len(ident) in (10, 13):
            found = await self.search_company_bankrupts(ident)
        elif len(ident) in (12, 15):
            found = await self.search_person_bankrupts(ident)
        else:
            raise ValueError(f"{ident!r}: нужен ИНН, ОГРН или ОГРНИП")
        return [b for b in found if _matches(b.raw, ident)]

    async def find_company_guid(self, inn: str) -> str | None:
        data = await self._get(FEDRESURS_URL, "/backend/companies",
                               {"searchString": inn, "limit": 15, "offset": 0})
        for rec in _rows(data):
            if _matches(rec, inn):
                return _first(rec, "guid", "id")
        return None

    async def company_publications(self, guid: str, limit: int = 50) -> list[Publication]:
        data = await self._get(FEDRESURS_URL, f"/backend/companies/{guid}/publications",
                               {"limit": limit, "offset": 0})
        out = []
        for rec in _rows(data):
            kind = _first(rec, "typeName", "type", "title", "messageType")
            if isinstance(kind, dict):
                kind = _first(kind, "name", "description", "title")
            out.append(Publication(type=str(kind or ""),
                                   date=_first(rec, "datePublish", "publishDate", "date"),
                                   number=_first(rec, "number"), guid=_first(rec, "guid", "id")))
        return out

    async def creditor_intentions(self, inn: str) -> list[Publication]:
        """Сообщения кредиторов о намерении подать заявление о банкротстве компании.

        Это ранний сигнал: дела ещё нет, но кредитор обязан предупредить
        за 15 дней до обращения в суд.
        """
        guid = await self.find_company_guid(inn)
        if not guid:
            return []
        return [p for p in await self.company_publications(guid) if _INTENTION.search(p.type)]
