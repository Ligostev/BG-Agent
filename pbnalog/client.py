"""Асинхронный клиент сервиса ФНС «Прозрачный бизнес» (pb.nalog.ru).

Официального API у сервиса нет, клиент повторяет запросы, которые делает
веб-интерфейс сайта:

1. POST /search-proc.json        поиск по ИНН/ОГРН/названию, в ответе token записи
2. POST /company-proc.json       method=get-request, по token получаем id и новый token
3. POST /company-proc.json       method=get-response, по id+token получаем карточку (JSON)

При частых запросах сайт требует капчу. Тогда клиент берёт картинку
(/captcha-dialog.html -> /static/captcha.bin), отдаёт её решателю, отправляет
ответ в /captcha-proc.json и повторяет запрос с полученным pbCaptchaToken.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from html import unescape
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

from .validators import classify

log = logging.getLogger(__name__)

BASE_URL = "https://pb.nalog.ru"

# Решатель капчи: получает картинку (bytes), возвращает распознанный текст.
CaptchaSolver = Callable[[bytes], Awaitable[str]]

DEFAULT_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": BASE_URL,
    "Referer": BASE_URL + "/",
}


class PbNalogError(Exception):
    pass


class NotFound(PbNalogError):
    pass


class CaptchaRequired(PbNalogError):
    """Сайт требует капчу, а решатель не задан или не справился."""


@dataclass
class SearchResult:
    ul: list[dict] = field(default_factory=list)   # юрлица
    ip: list[dict] = field(default_factory=list)   # ИП
    raw: dict = field(default_factory=dict)

    @property
    def all(self) -> list[dict]:
        return [*({"_kind": "ul", **r} for r in self.ul), *({"_kind": "ip", **r} for r in self.ip)]


def _search_payload(mode: str, query: str, page: int, page_size: int) -> dict[str, str]:
    # Набор полей как у формы на сайте; лишние пустые поля сервер игнорирует.
    p = {
        "page": str(page), "pageSize": str(page_size), "pbCaptchaToken": "", "token": "",
        "mode": mode, "queryAll": "", "queryUl": "", "okvedUl": "", "statusUl": "",
        "regionUl": "", "isMspUl": "", "queryIp": "", "okvedIp": "", "statusIp": "",
        "regionIp": "", "isMspIp": "", "mspIp1": "1", "mspIp2": "2", "mspIp3": "3",
        "queryUpr": "", "uprType1": "1", "uprType0": "1", "queryRdl": "", "dateRdl": "",
        "queryAddr": "", "regionAddr": "", "queryOgr": "", "ogrFl": "1", "ogrUl": "1",
        "npTypeDoc": "1", "ogrnUlDoc": "", "ogrnIpDoc": "", "nameUlDoc": "", "nameIpDoc": "",
        "formUlDoc": "", "formIpDoc": "", "ifnsDoc": "", "dateFromDoc": "", "dateToDoc": "",
    }
    p[{"search-all": "queryAll", "search-ul": "queryUl", "search-ip": "queryIp"}[mode]] = query
    return p


def _needs_captcha(resp: httpx.Response) -> bool:
    if resp.status_code == 200 and "captcha" not in resp.text[:2000].lower():
        return False
    return "captcha" in resp.text.lower()


class PbNalogClient:
    def __init__(
        self,
        *,
        solver: CaptchaSolver | None = None,
        min_interval: float = 1.5,
        timeout: float = 30.0,
        max_captcha_attempts: int = 3,
        poll_attempts: int = 10,
        poll_delay: float = 1.0,
        http: httpx.AsyncClient | None = None,
    ):
        self.solver = solver
        self.min_interval = min_interval
        self.max_captcha_attempts = max_captcha_attempts
        self.poll_attempts = poll_attempts
        self.poll_delay = poll_delay
        self._http = http or httpx.AsyncClient(
            base_url=BASE_URL, headers=DEFAULT_HEADERS, timeout=timeout, follow_redirects=True)
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        self._captcha_token = ""   # pbCaptchaToken, переиспользуем пока сайт его принимает

    async def __aenter__(self) -> "PbNalogClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- транспорт ---------------------------------------------------------

    async def _throttle(self) -> None:
        async with self._lock:
            wait = self._last_request + self.min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()

    async def _request(self, method: str, path: str, **kw) -> httpx.Response:
        await self._throttle()
        resp = await self._http.request(method, path, **kw)
        log.debug("%s %s -> %s %d bytes", method, path, resp.status_code, len(resp.content))
        return resp

    async def _post_json(self, path: str, data: dict[str, str], *, captcha: bool = True) -> Any:
        """POST формы с автоматическим прохождением капчи."""
        for attempt in range(self.max_captcha_attempts + 1):
            if captcha:
                data = {**data, "pbCaptchaToken": self._captcha_token}
            resp = await self._request("POST", path, data=data)
            if captcha and _needs_captcha(resp):
                if attempt == self.max_captcha_attempts:
                    break
                self._captcha_token = await self.solve_captcha()
                continue
            if resp.status_code != 200:
                raise PbNalogError(f"{path}: HTTP {resp.status_code}: {resp.text[:300]}")
            if not resp.text.strip():
                return None
            try:
                return resp.json()
            except ValueError as e:
                raise PbNalogError(f"{path}: ответ не JSON: {resp.text[:300]}") from e
        raise CaptchaRequired(f"{path}: капча не пройдена за {self.max_captcha_attempts} попыток")

    # --- капча -------------------------------------------------------------

    async def fetch_captcha(self) -> tuple[str, bytes]:
        """Возвращает (captchaToken, картинка)."""
        resp = await self._request("GET", "/captcha-dialog.html")
        m = re.search(r'<img[^>]+src="([^"]*captcha[^"]*)"', resp.text, re.I)
        if not m:
            raise PbNalogError("не нашли картинку капчи на captcha-dialog.html")
        src = urljoin(BASE_URL + "/", unescape(m.group(1)))
        token = parse_qs(urlparse(src).query).get("a", [""])[0]
        if not token:
            raise PbNalogError(f"в ссылке на капчу нет токена: {src}")
        img = await self._request("GET", src)
        return token, img.content

    async def submit_captcha(self, captcha_token: str, answer: str) -> str | None:
        """Отправляет ответ; возвращает pbCaptchaToken или None, если ответ неверный."""
        resp = await self._request("POST", "/captcha-proc.json",
                                   data={"captcha": answer, "captchaToken": captcha_token})
        if resp.status_code != 200:
            return None
        return resp.text.strip().strip('"') or None

    async def solve_captcha(self) -> str:
        if self.solver is None:
            raise CaptchaRequired("сайт требует капчу, а решатель (solver) не задан")
        for _ in range(self.max_captcha_attempts):
            token, image = await self.fetch_captcha()
            answer = (await self.solver(image)).strip()
            pb_token = await self.submit_captcha(token, answer)
            if pb_token:
                return pb_token
            log.info("капча решена неверно, пробуем ещё раз")
        raise CaptchaRequired("решатель не справился с капчей")

    # --- API ---------------------------------------------------------------

    async def search(self, query: str, kind: str = "all", page: int = 1,
                     page_size: int = 10) -> SearchResult:
        """kind: all | ul | ip."""
        mode = {"all": "search-all", "ul": "search-ul", "ip": "search-ip"}[kind]
        raw = await self._post_json("/search-proc.json", _search_payload(mode, query, page, page_size)) or {}
        return SearchResult(
            ul=(raw.get("ul") or {}).get("data") or [],
            ip=(raw.get("ip") or {}).get("data") or [],
            raw=raw,
        )

    async def get_company(self, token: str) -> dict:
        """Полная карточка ЮЛ или ИП по token из результатов поиска."""
        req = await self._post_json("/company-proc.json", {"token": token, "method": "get-request"})
        if not req or "id" not in req:
            raise PbNalogError(f"company-proc get-request: неожиданный ответ {req!r}")
        payload = {"id": str(req["id"]), "token": req.get("token", token), "method": "get-response"}
        for _ in range(self.poll_attempts):
            data = await self._post_json("/company-proc.json", payload, captcha=False)
            # Пока карточка готовится, сайт отдаёт пустой ответ или крошечный JSON.
            if isinstance(data, dict) and len(data) > 1:
                return data
            await asyncio.sleep(self.poll_delay)
        raise PbNalogError("карточка не готова: превышено число попыток get-response")

    async def lookup(self, query: str) -> dict:
        """Поиск по ИНН/ОГРН и загрузка карточки первой найденной записи.

        Возвращает {"kind": "ul"|"ip", "hit": <строка поиска>, "card": <карточка>}.
        """
        q = re.sub(r"\D", "", query)
        id_type = classify(q)
        if id_type is None:
            raise ValueError(f"{query!r} не похоже на корректный ИНН или ОГРН")
        kind = "ul" if id_type in ("inn_ul", "ogrn") else "ip"
        res = await self.search(q, kind=kind)
        rows = res.ul if kind == "ul" else res.ip
        # Точное совпадение важнее порядка выдачи (по ИНН может найтись и филиал/история).
        hit = next((r for r in rows if q in (str(r.get("inn", "")), str(r.get("ogrn", "")))), None)
        hit = hit or (rows[0] if rows else None)
        if hit is None:
            raise NotFound(f"по {q} ничего не найдено")
        card = await self.get_company(hit["token"])
        return {"kind": kind, "hit": hit, "card": card}
