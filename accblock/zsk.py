"""Банк России, платформа «Знай своего клиента»: проверка по ИНН.

С 1 октября 2024 года на сайте ЦБ любой желающий может узнать, отнесена ли
компания или ИП к группе высокого уровня риска (страница ZSK_PAGE). Красная
зона означает, что банки ограничивают клиенту операции и открытие счетов
по 115-ФЗ; физлица в сервисе не учитываются.

Как именно форма отправляет запрос (адрес, параметры, есть ли капча), из облака
Claude проверить нельзя: cbr.ru оттуда не открывается. Поэтому автоматический
запрос включается только переменной CBR_ZSK_URL, когда адрес будет найден
на сервере (`python -m accblock --zsk-page`). Без неё отчёт даёт ссылку на
форму для ручной проверки, и проверка не падает.

CBR_ZSK_URL — адрес запроса; `{inn}` в нём заменяется на ИНН, иначе ИНН
передаётся параметром CBR_ZSK_PARAM (по умолчанию inn).
"""

from __future__ import annotations

import os
import re
from typing import Any

import httpx

ZSK_PAGE = "https://www.cbr.ru/counteraction_m_ter/platform_zsk/proverka-po-inn/"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"),
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Referer": ZSK_PAGE,
}


class ZskError(Exception):
    pass


_NOT_HIGH = re.compile(r"не\s+(отнесен|относит|включ|является)[^.]{0,80}высок"
                       r"|высок[^.]{0,40}риск[^.]{0,40}(не\s+(установлен|присвоен)|отсутств)"
                       r"|(низк|средн)[а-я]*\s+(уров|риск)", re.I)
_HIGH = re.compile(r"(отнесен|относится|включен|присвоен)[^.]{0,80}высок[а-я]*\s+(уров|риск)"
                   r"|высок[а-я]*\s+уров[а-я]*\s+риск|красн[а-я]*\s+зон", re.I)


def _text(payload: Any) -> str:
    if isinstance(payload, dict):
        return " ".join(_text(v) for v in payload.values())
    if isinstance(payload, list):
        return " ".join(_text(v) for v in payload)
    return "" if payload is None else str(payload)


def parse_zsk(payload: Any) -> tuple[bool | None, str]:
    """(высокий риск: True/False/None — не поняли, фраза из ответа)."""
    if isinstance(payload, dict):
        for key in ("isHighRisk", "highRisk", "high_risk", "isRed", "red"):
            if isinstance(payload.get(key), bool):
                return payload[key], _text(payload).strip()[:300]
    text = re.sub(r"<[^>]+>", " ", _text(payload))
    text = re.sub(r"\s+", " ", text).strip()
    m = _NOT_HIGH.search(text)
    if m:
        return False, _sentence(text, m.start())
    m = _HIGH.search(text)
    if m:
        return True, _sentence(text, m.start())
    return None, text[:300]


def _sentence(text: str, pos: int) -> str:
    start = text.rfind(".", 0, pos) + 1
    end = text.find(".", pos)
    return text[start:end + 1 if end >= 0 else len(text)].strip()[:300]


class ZskClient:
    def __init__(self, url: str | None = None, param: str | None = None, timeout: float = 20,
                 http: httpx.AsyncClient | None = None):
        self.url = url if url is not None else os.environ.get("CBR_ZSK_URL", "").strip()
        self.param = param or os.environ.get("CBR_ZSK_PARAM", "inn")
        self._http = http or httpx.AsyncClient(headers=HEADERS, timeout=timeout,
                                               follow_redirects=True)
        self.last_raw: Any = None

    @property
    def configured(self) -> bool:
        return bool(self.url)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def page(self) -> str:
        """HTML формы — чтобы на сервере найти, куда она отправляет запрос."""
        resp = await self._http.get(ZSK_PAGE)
        resp.raise_for_status()
        return resp.text

    async def check(self, inn: str) -> tuple[bool | None, str]:
        if not self.configured:
            raise ZskError("адрес запроса не задан (CBR_ZSK_URL)")
        if "{inn}" in self.url:
            resp = await self._http.get(self.url.replace("{inn}", inn))
        else:
            resp = await self._http.get(self.url, params={self.param: inn})
        resp.raise_for_status()
        try:
            payload: Any = resp.json()
        except ValueError:
            payload = resp.text
            if re.search(r"captcha|капч", payload[:5000], re.I):
                raise ZskError("сайт ЦБ просит капчу") from None
        self.last_raw = payload
        return parse_zsk(payload)


def find_endpoints(html: str) -> list[str]:
    """Кандидаты в адрес запроса: action форм и пути с api/zsk/inn в скриптах."""
    found = re.findall(r'<form[^>]*action="([^"]+)"', html, re.I)
    found += re.findall(r'["\'](/[^"\'\s]*(?:api|zsk|inn|risk)[^"\'\s]*)["\']', html, re.I)
    seen: list[str] = []
    for f in found:
        if f not in seen:
            seen.append(f)
    return seen
