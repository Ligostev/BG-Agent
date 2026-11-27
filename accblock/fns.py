"""Клиент сервиса ФНС о действующих решениях о приостановлении операций по счетам.

Страница: https://service.nalog.ru/bi.do («Запрос о действующих решениях о
приостановлении»). На вход ИНН налогоплательщика и БИК банка, на выходе список
действующих решений. Запрос, который делает сама страница:

    POST https://service.nalog.ru/bi2-proc.json
         requestType=FINDPRS&innPRS=<ИНН>&bikPRS=<БИК>&captcha=<цифры>&captchaToken=<токен>

Капча: токен из /static/captcha.html, картинка /static/captcha.bin?a=<токен>.
Капча не обходится: картинку видит пользователь в кабинете (как у pb.nalog.ru).

Из облака Claude сайт ФНС недоступен, поэтому формат ответа не сверен с живым
сервисом: поля решений разбираются с запасными вариантами, а непонятный ответ
не считается «решений нет» (FnsFormatError). Сверить на сервере:
`python -m accblock <ИНН> --dump`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://service.nalog.ru"
PAGE_URL = BASE_URL + "/bi.do"
# БИК нужен форме. По нашему пониманию сервис показывает решения по всем банкам,
# а БИК — это банк, который спрашивает; на сервере это надо подтвердить (README).
DEFAULT_BIK = "044525225"  # ПАО Сбербанк

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": BASE_URL,
    "Referer": PAGE_URL,
}

CaptchaSolver = Callable[[bytes], Awaitable[str]]


class FnsBlocksError(Exception):
    pass


class FnsFormatError(FnsBlocksError):
    """Ответ пришёл, но разобрать его не удалось — это не «решений нет»."""


class CaptchaRequired(FnsBlocksError):
    pass


@dataclass
class BlockDecision:
    """Действующее решение о приостановлении операций по счетам."""
    number: str | None = None      # номер решения
    date: str | None = None        # дата решения
    tax_office: str | None = None  # код налогового органа
    bik: str | None = None         # БИК банка, где приостановлены операции
    bank: str | None = None
    posted_at: str | None = None   # когда сведения размещены
    raw: dict = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("raw")
        return d

    def label(self) -> str:
        head = f"№ {self.number}" if self.number else "решение"
        parts = [f"{head} от {self.date}" if self.date else head]
        if self.tax_office:
            parts.append(f"ИФНС {self.tax_office}")
        if self.bank or self.bik:
            parts.append(f"банк {self.bank or ''}{' (БИК ' + self.bik + ')' if self.bik else ''}".strip())
        return ", ".join(parts)


# Имена полей в ответе не сверены: ключ решения ищем по нескольким вариантам.
_FIELDS = {
    "number": ("nomer", "nomresh", "number", "num", "nomerresh"),
    "date": ("data", "dataresh", "date", "datersh"),
    "tax_office": ("ifns", "kodno", "kodifns", "no", "codeno", "kod_no"),
    "bik": ("bik", "bikbank"),
    "bank": ("naimbank", "bank", "bankname", "naimbnk"),
    "posted_at": ("databi", "dateplace", "datarazm", "datebi"),
}
_ROWS_KEYS = ("rows", "data", "items", "result", "decisions", "prs")
_NONE_TEXT = re.compile(r"(нет|не найден|отсутству)[^.]{0,80}(решени|приостановлен)"
                        r"|(решени|приостановлен)[^.]{0,80}(нет|не найден|отсутству)", re.I)


def _norm(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _decision(row: dict) -> BlockDecision:
    by_key = {_norm(k): v for k, v in row.items()}
    out: dict[str, str | None] = {}
    for attr, names in _FIELDS.items():
        value = next((by_key[n] for n in names if by_key.get(n) not in (None, "")), None)
        out[attr] = str(value).strip() if value is not None else None
    return BlockDecision(**out, raw=row)


def parse_decisions(payload: Any) -> list[BlockDecision]:
    """Разбор ответа bi2-proc.json. Пустой список — решений нет.

    Бросает FnsFormatError, если в ответе нет ни списка решений, ни текста
    о том, что решений нет, и CaptchaRequired, если сервис просит капчу.
    """
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        errors = payload.get("ERRORS") or payload.get("errors")
        if errors:
            text = str(errors)
            if "captcha" in text.lower() or "символ" in text.lower():
                raise CaptchaRequired(text)
            raise FnsBlocksError(f"сервис вернул ошибку: {text[:200]}")
        if payload.get("captchaRequired"):
            raise CaptchaRequired("сервис просит капчу")
        rows = None
        for key in _ROWS_KEYS:
            for k, v in payload.items():
                if k.lower() == key and isinstance(v, list):
                    rows = v
                    break
            if rows is not None:
                break
        if rows is None:
            text = " ".join(str(v) for v in payload.values() if isinstance(v, str))
            if _NONE_TEXT.search(text):
                return []
            raise FnsFormatError(f"не нашли список решений, ключи ответа: {sorted(payload)[:20]}")
    else:
        raise FnsFormatError(f"неожиданный ответ: {type(payload).__name__}")
    return [_decision(r) for r in rows if isinstance(r, dict)]


class FnsBlocksClient:
    def __init__(self, bik: str | None = None, timeout: float = 20, min_interval: float = 1.0,
                 max_captcha_attempts: int = 3, http: httpx.AsyncClient | None = None):
        self.bik = bik or os.environ.get("FNS_BI_BIK") or DEFAULT_BIK
        self.min_interval = min_interval
        self.max_captcha_attempts = max_captcha_attempts
        self._http = http or httpx.AsyncClient(base_url=BASE_URL, headers=HEADERS, timeout=timeout,
                                               follow_redirects=True)
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._warmed = False
        self.last_raw: Any = None  # последний ответ, для --dump

    async def __aenter__(self) -> "FnsBlocksClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kw) -> httpx.Response:
        async with self._lock:  # сервис ФНС не любит частые запросы
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return await self._http.request(method, path, **kw)
            finally:
                self._last = time.monotonic()

    async def fetch_captcha(self) -> tuple[str, bytes]:
        ts = str(int(time.time() * 1000))
        resp = await self._request("GET", "/static/captcha.html", params={"r": ts, "version": "2"})
        resp.raise_for_status()
        token = resp.text.strip()
        if not re.fullmatch(r"[A-Za-z0-9_\-]{10,}", token):
            raise FnsBlocksError("не получили токен капчи")
        img = await self._request("GET", "/static/captcha.bin",
                                  params={"r": ts, "a": token, "version": "2"})
        img.raise_for_status()
        return token, img.content

    async def _post(self, inn: str, bik: str, captcha: str = "", token: str = "") -> Any:
        data = {"requestType": "FINDPRS", "innPRS": inn, "bikPRS": bik, "fileName": "",
                "bik": "", "kodTU": "", "dateSAFN": "", "bikAFN": "", "dateAFN": "",
                "fileNameED": "", "captcha": captcha, "captchaToken": token}
        resp = await self._request("POST", "/bi2-proc.json", data=data)
        try:
            payload = resp.json()
        except ValueError:
            if "captcha" in resp.text[:3000].lower():
                raise CaptchaRequired("сервис просит капчу") from None
            resp.raise_for_status()
            raise FnsFormatError(f"ответ не JSON (HTTP {resp.status_code})") from None
        self.last_raw = payload
        if resp.status_code >= 500:
            resp.raise_for_status()
        return payload

    async def decisions(self, inn: str, solver: CaptchaSolver | None = None,
                        bik: str | None = None) -> list[BlockDecision]:
        inn = re.sub(r"\D", "", inn)
        if len(inn) not in (10, 12):
            raise ValueError("нужен ИНН из 10 или 12 цифр")
        bik = bik or self.bik
        if not self._warmed:  # куки сессии, как у браузера
            try:
                await self._request("GET", "/bi.do")
            except httpx.HTTPError as e:
                log.debug("bi.do: %s", e)
            self._warmed = True
        try:
            return parse_decisions(await self._post(inn, bik))
        except CaptchaRequired:
            if solver is None:
                raise
        for _ in range(self.max_captcha_attempts):
            token, image = await self.fetch_captcha()
            answer = (await solver(image)).strip()
            try:
                return parse_decisions(await self._post(inn, bik, answer, token))
            except CaptchaRequired:
                log.info("капча ФНС не принята, ещё попытка")
        raise CaptchaRequired(f"капча не пройдена за {self.max_captcha_attempts} попыток")
