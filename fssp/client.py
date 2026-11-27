"""Асинхронный клиент банка данных исполнительных производств ФССП.

Открытый API ФССП (api-ip.fssp.gov.ru) закрыт, поэтому клиент повторяет поиск
со страницы https://fssp.gov.ru/iss/ip:

    GET https://is.fssp.gov.ru/ajax_search?system=ip&is[extended]=1&is[variant]=...

Варианты поиска: 1 — физлицо (ФИО, дата рождения), 2 — юрлицо (наименование,
адрес), 3 — номер исполнительного производства. По ИНН сайт не ищет, поэтому
юрлицо ищем по названию, ИП — по ФИО.

Ответ — JSON (или JSONP) с HTML в поле ``data``. В HTML либо таблица из восьми
колонок, либо форма капчи: картинка ``<img id="capchaVisual" src="data:image/...;base64,...">``.
Тогда картинку отдаём решателю и повторяем тот же запрос с ``code=<ответ>``.
Капча из кириллических букв и цифр.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from typing import Awaitable, Callable

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

BASE_URL = "https://is.fssp.gov.ru"
SEARCH_PATH = "/ajax_search"
ALL_REGIONS = -1

CaptchaSolver = Callable[[bytes], Awaitable[str]]

DEFAULT_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://fssp.gov.ru/iss/ip",
}


class FsspError(Exception):
    pass


class CaptchaRequired(FsspError):
    """Сайт требует капчу, а решатель не задан или не справился."""


@dataclass
class Proceeding:
    debtor: str            # должник: наименование и адрес / ФИО и дата рождения
    debtor_name: str       # первая строка ячейки должника: наименование или ФИО
    proceeding: str        # номер и дата возбуждения ИП
    document: str          # реквизиты исполнительного документа
    ended: str             # дата и причина окончания; пусто — производство ведётся
    subject: str           # предмет исполнения и остаток долга
    department: str        # отдел судебных приставов
    bailiff: str           # судебный пристав-исполнитель
    amount: float | None   # непогашенный остаток, руб. (из колонки «предмет исполнения»)

    @property
    def active(self) -> bool:
        return not self.ended

    def to_dict(self) -> dict:
        return {**asdict(self), "active": self.active}


def _text(node) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def parse_amount(subject: str) -> float | None:
    """Сумма из «Задолженность по налогам: 1 234,56 руб.»; несколько сумм складываем."""
    found = re.findall(r"(\d[\d\s ]*(?:[.,]\d{1,2})?)\s*руб", subject)
    if not found:
        return None
    return round(sum(float(re.sub(r"[\s ]", "", s).replace(",", ".")) for s in found), 2)


def parse_table(html: str) -> list[Proceeding]:
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.select("table tr"):
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 8:      # заголовок или строка с названием региона
            continue
        # Колонка 5 — «Сервис» (ссылка на оплату), она нам не нужна.
        debtor, proc, doc, ended, _service, subject, dept, bailiff = (_text(c) for c in cells[:8])
        lines = [ln for ln in (s.strip() for s in cells[0].get_text("\n").splitlines()) if ln]
        rows.append(Proceeding(debtor, " ".join((lines or [""])[0].split()), proc, doc, ended,
                               subject, dept, bailiff, parse_amount(subject)))
    return rows


def _captcha_image(html: str) -> bytes | None:
    m = re.search(r'<img[^>]*id="capchaVisual"[^>]*src="data:image/[a-z]+;base64,([^"]+)"', html, re.I) \
        or re.search(r'<img[^>]*src="data:image/[a-z]+;base64,([^"]+)"[^>]*id="capchaVisual"', html, re.I)
    return base64.b64decode(m.group(1)) if m else None


def _has_next_page(html: str, page: int) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    return any(_text(a) == str(page + 1) for a in soup.select(".pagination a"))


def _unwrap(text: str) -> str:
    """Достаёт HTML из ответа: JSON, JSONP (callback({...});) или сразу HTML."""
    body = text.strip()
    m = re.fullmatch(r"[\w$.]+\((.*)\);?", body, re.S)
    if m:
        body = m.group(1)
    if body.startswith("{"):
        try:
            data = json.loads(body)
        except ValueError as e:
            raise FsspError(f"ответ не JSON: {body[:300]}") from e
        if data.get("err"):
            raise FsspError(f"ФССП: {data['err']}")
        return data.get("data") or ""
    return body


class FsspClient:
    def __init__(self, *, solver: CaptchaSolver | None = None, min_interval: float = 2.0,
                 timeout: float = 30.0, max_captcha_attempts: int = 3, max_pages: int = 5,
                 http: httpx.AsyncClient | None = None):
        self.solver = solver
        self.min_interval = min_interval
        self.max_captcha_attempts = max_captcha_attempts
        self.max_pages = max_pages
        self._http = http or httpx.AsyncClient(
            base_url=BASE_URL, headers=DEFAULT_HEADERS, timeout=timeout, follow_redirects=True)
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    async def __aenter__(self) -> "FsspClient":
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

    async def _get_page(self, params: dict[str, str]) -> str:
        """Одна страница выдачи, с прохождением капчи."""
        code = ""
        for attempt in range(self.max_captcha_attempts + 1):
            await self._throttle()
            resp = await self._http.get(SEARCH_PATH, params={**params, "code": code} if code else params)
            log.debug("GET %s -> %s %d bytes", resp.url, resp.status_code, len(resp.content))
            if resp.status_code != 200:
                raise FsspError(f"is.fssp.gov.ru: HTTP {resp.status_code}")
            html = _unwrap(resp.text)
            image = _captcha_image(html)
            if image is None:
                return html
            if self.solver is None:
                raise CaptchaRequired("сайт ФССП требует капчу, а решатель (solver) не задан")
            if attempt == self.max_captcha_attempts:
                break
            if attempt:
                log.info("капча ФССП решена неверно, пробуем ещё раз")
            code = (await self.solver(image)).strip()
        raise CaptchaRequired(f"капча ФССП не пройдена за {self.max_captcha_attempts} попыток")

    async def _search(self, variant: int, fields: dict[str, str], region: int) -> list[Proceeding]:
        params = {"system": "ip", "is[extended]": "1", "nocache": "1",
                  "is[variant]": str(variant), "is[region_id][0]": str(region)}
        params.update({f"is[{k}]": v for k, v in fields.items()})
        result: list[Proceeding] = []
        for page in range(1, self.max_pages + 1):
            html = await self._get_page({**params, "page": str(page)})
            result += parse_table(html)
            if not _has_next_page(html, page):
                break
        return result

    async def search_legal(self, name: str, address: str = "",
                           region: int = ALL_REGIONS) -> list[Proceeding]:
        """Производства, где должник — юрлицо с таким наименованием."""
        return await self._search(2, {"drtr_name": name, "address": address}, region)

    async def search_person(self, last_name: str, first_name: str, patronymic: str = "",
                            birth_date: str = "", region: int = ALL_REGIONS) -> list[Proceeding]:
        """Производства против физлица или ИП; birth_date в формате ДД.ММ.ГГГГ."""
        return await self._search(1, {"last_name": last_name, "first_name": first_name,
                                      "patronymic": patronymic, "date": birth_date}, region)

    async def search_number(self, ip_number: str) -> list[Proceeding]:
        """Поиск по номеру производства, например 12345/24/77001-ИП."""
        return await self._search(3, {"ip_number": ip_number}, ALL_REGIONS)
