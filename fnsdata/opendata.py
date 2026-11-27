"""Скачивание наборов открытых данных ФНС.

У каждого набора есть паспорт nalog.gov.ru/opendata/<код>/meta.csv, в нём ссылка на
актуальный файл вида file.nalog.ru/opendata/<код>/data-ГГГГММДД-structure-ГГГГММДД.csv|zip.
Имя файла меняется при каждом обновлении, поэтому сначала читается паспорт, и файл
скачивается заново только если ссылка поменялась. Капчи нет; сайты ФНС открываются
только с российских IP. Формат паспорта не сверен с живым сайтом (облако Claude туда
не пускают): ссылка ищется регулярным выражением по всему тексту паспорта.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

META_URL = "https://www.nalog.gov.ru/opendata/{code}/meta.csv"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"),
    "Accept-Language": "ru-RU,ru;q=0.9",
}
_DATA = re.compile(r"https?://[^\s;\",']+/data-(\d{8})[^\s;\",']*\.(?:csv|zip|xml|xlsx)", re.I)


class OpenDataError(Exception):
    pass


def find_data_url(meta: str) -> str | None:
    """Ссылка на самый свежий файл данных из паспорта набора."""
    found = [(m.group(1), m.group(0)) for m in _DATA.finditer(meta)]
    return max(found)[1] if found else None


class OpenDataset:
    """Локальная копия набора: <cache_dir>/<код>.<расширение> и рядом .url с источником."""

    def __init__(self, code: str, cache_dir: str | os.PathLike, http: httpx.AsyncClient | None = None,
                 ttl: float = 24 * 3600, data_url: str | None = None, timeout: float = 300):
        self.code = code
        self.cache_dir = Path(cache_dir)
        self.http = http or httpx.AsyncClient(headers=HEADERS, timeout=timeout, follow_redirects=True)
        self.ttl = ttl
        self.data_url = data_url
        self._checked = 0.0
        self._lock = asyncio.Lock()
        self.stale_reason: str | None = None

    @property
    def _url_file(self) -> Path:
        return self.cache_dir / f"{self.code}.url"

    def local(self) -> Path | None:
        for p in sorted(self.cache_dir.glob(f"{self.code}.*")):
            if p.suffix != ".url" and not p.name.endswith(".part") and not p.name.endswith(".db"):
                return p
        return None

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _get(self, url: str) -> httpx.Response:
        try:
            r = await self.http.get(url)
        except httpx.HTTPError as e:
            raise OpenDataError(f"{url}: {type(e).__name__}") from e
        if r.status_code != 200:
            raise OpenDataError(f"{url}: HTTP {r.status_code}")
        return r

    async def _refresh(self) -> Path:
        url = self.data_url
        if not url:
            meta = await self._get(META_URL.format(code=self.code))
            url = find_data_url(meta.text)
            if not url:
                raise OpenDataError(f"в паспорте набора {self.code} не найдена ссылка на данные")
        old = self.local()
        if old and self._url_file.exists() and self._url_file.read_text().strip() == url:
            return old
        ext = Path(url.split("?")[0]).suffix.lower() or ".bin"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        part = self.cache_dir / f"{self.code}{ext}.part"
        try:
            async with self.http.stream("GET", url) as r:
                if r.status_code != 200:
                    raise OpenDataError(f"{url}: HTTP {r.status_code}")
                with part.open("wb") as f:
                    async for chunk in r.aiter_bytes(1 << 20):
                        f.write(chunk)
        except httpx.HTTPError as e:
            part.unlink(missing_ok=True)
            raise OpenDataError(f"{url}: {type(e).__name__}") from e
        if old:
            old.unlink(missing_ok=True)
        dest = self.cache_dir / f"{self.code}{ext}"
        part.rename(dest)
        self._url_file.write_text(url)
        log.info("набор ФНС %s обновлён: %s", self.code, url)
        return dest

    async def path(self) -> Path:
        """Путь к свежей (не старше ttl) локальной копии; при ошибке сайта — к старой копии."""
        async with self._lock:
            have = self.local()
            if have and time.monotonic() - self._checked < self.ttl:
                return have
            try:
                p = await self._refresh()
            except Exception as e:
                if not have:
                    raise
                self.stale_reason = f"{type(e).__name__}: {e}"
                log.warning("набор ФНС %s не обновлён, используется старая копия: %s",
                            self.code, self.stale_reason)
                self._checked = time.monotonic() - self.ttl + 600  # повтор не раньше чем через 10 минут
                return have
            self._checked, self.stale_reason = time.monotonic(), None
            return p

    def source_url(self) -> str | None:
        return self._url_file.read_text().strip() if self._url_file.exists() else None
