"""Загрузка реестра ст. 19.28 КоАП из ЕИС.

Файл лежит в разделе «Документы» ЕИС:

    GET zakupki.gov.ru/epz/main/public/document/view.html?sectionId=2369   страница раздела
    GET zakupki.gov.ru/epz/main/public/download/downloadDocument.html?id=<id>  сам файл .xlsx

Капчи и токена нет, но ЕИС открывается только с российских IP. Ссылка на файл
меняется при каждом обновлении, поэтому сначала читается страница раздела.
Структура страницы не сверена с живым сайтом: берётся ссылка на скачивание,
перед которой (или в которой) написано «19.28», иначе ссылка с наибольшим id.

Файл небольшой (около 1000 строк), поэтому он скачивается целиком и держится
в памяти, обновляется раз в ttl. Последняя удачная копия сохраняется на диск
(cache_path), чтобы проверки работали, пока ЕИС недоступна.

Переменные окружения:
    KOAP1928_FILE   путь к локальному .xlsx (скачан вручную) — сайт не опрашивается
    KOAP1928_URL    прямая ссылка на файл, если страница раздела поменялась
    KOAP1928_CACHE  куда сохранять последнюю копию (по умолчанию не сохранять)
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

import httpx

from .registry import Registry, parse_xlsx

log = logging.getLogger(__name__)

EIS_URL = "https://zakupki.gov.ru"
SECTION_PATH = "/epz/main/public/document/view.html"
SECTION_ID = "2369"
PAGE_URL = f"{EIS_URL}{SECTION_PATH}?searchString=&sectionId={SECTION_ID}&strictEqual=false"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"),
    "Accept-Language": "ru-RU,ru;q=0.9",
}

_LINK = re.compile(r"""href=["']([^"']*downloadDocument\.html\?(?:[^"']*&(?:amp;)?)?id=(\d+)[^"']*)["']""",
                   re.I)


class RegistryUnavailable(Exception):
    pass


def find_file_url(page: str, base: str = EIS_URL) -> str | None:
    """Ссылка на файл реестра на странице раздела «Документы» ЕИС."""
    links, prev_end = [], 0
    for m in _LINK.finditer(page):
        # подпись документа стоит перед ссылкой или внутри неё: текст от прошлой ссылки до этой
        end = page.find("</a>", m.end())
        label = page[prev_end: end if end != -1 else m.end()]
        prev_end = m.end()
        links.append((("19.28" in label), int(m.group(2)), html.unescape(m.group(1))))
    if not links:
        return None
    _, _, href = max(links)
    return href if href.startswith("http") else base + ("" if href.startswith("/") else "/") + href


class Koap1928Client:
    def __init__(self, http: httpx.AsyncClient | None = None, ttl: float = 12 * 3600,
                 file_path: str | None = None, file_url: str | None = None,
                 cache_path: str | None = None, timeout: float = 60):
        self.http = http or httpx.AsyncClient(headers=HEADERS, timeout=timeout,
                                              follow_redirects=True)
        self.ttl = ttl
        self.file_path = file_path if file_path is not None else os.environ.get("KOAP1928_FILE") or None
        self.file_url = file_url if file_url is not None else os.environ.get("KOAP1928_URL") or None
        self.cache_path = cache_path if cache_path is not None else os.environ.get("KOAP1928_CACHE") or None
        self._registry: Registry | None = None
        self._fetched = 0.0
        self._lock = asyncio.Lock()
        self.stale_reason: str | None = None   # почему отдана старая копия

    async def __aenter__(self) -> "Koap1928Client":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _get(self, url: str) -> httpx.Response:
        try:
            r = await self.http.get(url, headers={"Referer": PAGE_URL})
        except httpx.HTTPError as e:
            raise RegistryUnavailable(f"ЕИС не ответила: {type(e).__name__}") from e
        if r.status_code != 200:
            raise RegistryUnavailable(f"ЕИС: HTTP {r.status_code} на {url}")
        return r

    async def download(self) -> tuple[bytes, str]:
        """Скачать актуальный файл: (содержимое, ссылка)."""
        url = self.file_url
        if not url:
            page = await self._get(PAGE_URL)
            url = find_file_url(page.text)
            if not url:
                raise RegistryUnavailable("на странице ЕИС не найдена ссылка на файл реестра 19.28")
        r = await self._get(url)
        return r.content, url

    def _load_local(self, path: str, source: str) -> Registry:
        content = Path(path).read_bytes()
        loaded = datetime.fromtimestamp(Path(path).stat().st_mtime)
        return Registry(parse_xlsx(content), loaded_at=loaded, source=source)

    async def registry(self) -> Registry:
        """Реестр, обновлённый не раньше чем ttl назад."""
        async with self._lock:
            if self._registry is not None and time.monotonic() - self._fetched < self.ttl:
                return self._registry
            if self.file_path:
                self._registry = self._load_local(self.file_path, f"файл {self.file_path}")
                self._fetched = time.monotonic()
                return self._registry
            try:
                content, url = await self.download()
                reg = Registry(parse_xlsx(content), source=url)
            except Exception as e:
                reason = f"{type(e).__name__}: {e}"
                if self._registry is not None:
                    log.warning("реестр 19.28 не обновлён, работаем по старой копии: %s", reason)
                elif self.cache_path and Path(self.cache_path).exists():
                    log.warning("реестр 19.28 из ЕИС недоступен, берём копию с диска: %s", reason)
                    self._registry = self._load_local(self.cache_path, "сохранённая копия")
                else:
                    raise
                self.stale_reason = reason
                # следующая попытка не раньше чем через 10 минут, чтобы не долбить ЕИС
                self._fetched = time.monotonic() - self.ttl + 600
                return self._registry
            if self.cache_path:
                Path(self.cache_path).parent.mkdir(parents=True, exist_ok=True)
                Path(self.cache_path).write_bytes(content)
            self._registry, self._fetched, self.stale_reason = reg, time.monotonic(), None
            log.info("реестр 19.28 загружен: %s записей из %s", len(reg), url)
            return reg
