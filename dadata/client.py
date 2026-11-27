"""Клиент API подсказок DaData.

Ключ берётся из DADATA_API_KEY (и, если понадобится, DADATA_SECRET для методов
«Стандартизации»; для findById/party секрет не нужен). Ответы кешируются в
памяти: бесплатный лимит — 10 000 запросов в сутки, а одну компанию часто
проверяют несколько раз подряд.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx

BASE_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs"


class DaDataError(Exception):
    pass


class DaDataAuthError(DaDataError):
    """Ключ не задан, неверный или заблокирован (401/403)."""


class DaDataLimitError(DaDataError):
    """Слишком много запросов или исчерпан суточный лимит (429)."""


class DaDataClient:
    def __init__(self, api_key: str | None = None, secret: str | None = None, *,
                 base_url: str = BASE_URL, timeout: float = 10.0,
                 cache_ttl: float = 3600.0, cache_size: int = 1000, retries: int = 1,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.api_key = (api_key if api_key is not None else os.environ.get("DADATA_API_KEY", "")).strip()
        self.secret = (secret if secret is not None else os.environ.get("DADATA_SECRET", "")).strip()
        self.base_url = base_url.rstrip("/")
        self.cache_ttl = cache_ttl
        self.cache_size = cache_size
        self.retries = retries
        self._cache: dict[tuple, tuple[float, Any]] = {}
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post(self, method: str, body: dict) -> dict:
        if not self.api_key:
            raise DaDataAuthError("не задан ключ API (переменная DADATA_API_KEY)")
        headers = {"Authorization": f"Token {self.api_key}", "Accept": "application/json"}
        if self.secret:
            headers["X-Secret"] = self.secret
        url = f"{self.base_url}/{method}"
        for attempt in range(self.retries + 1):
            try:
                resp = await self._http.post(url, json=body, headers=headers)
            except httpx.TransportError as e:
                if attempt < self.retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise DaDataError(f"сеть: {type(e).__name__}: {e}") from e
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (401, 403):
                raise DaDataAuthError(
                    f"HTTP {resp.status_code}: ключ неверный, не активирован или исчерпан лимит")
            if resp.status_code == 429:
                if attempt < self.retries:
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue
                raise DaDataLimitError("HTTP 429: слишком много запросов")
            if resp.status_code >= 500 and attempt < self.retries:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            raise DaDataError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        raise DaDataError("нет ответа")  # pragma: no cover

    async def _cached(self, key: tuple, method: str, body: dict) -> dict:
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and now - hit[0] < self.cache_ttl:
            return hit[1]
        data = await self._post(method, body)
        if len(self._cache) >= self.cache_size:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = (now, data)
        return data

    async def find_party(self, query: str, *, branch_type: str | None = "MAIN",
                         kind: str | None = None) -> dict | None:
        """Головная организация или ИП по ИНН/ОГРН: объект suggestion или None.

        kind: LEGAL или INDIVIDUAL — нужен, когда ИНН физлица совпадает с ИП.
        """
        body: dict[str, Any] = {"query": query, "count": 1}
        if branch_type:
            body["branch_type"] = branch_type
        if kind:
            body["type"] = kind
        data = await self._cached(("party", query, branch_type, kind), "findById/party", body)
        found = data.get("suggestions") or []
        return found[0] if found else None

    async def find_affiliated(self, inn: str, *, scope: list[str] | None = None,
                              count: int = 20) -> list[dict]:
        """Компании, где лицо с этим ИНН учредитель или руководитель (тариф «Максимальный»)."""
        body: dict[str, Any] = {"query": inn, "count": count}
        if scope:
            body["scope"] = scope
        data = await self._cached(("affiliated", inn, tuple(scope or ()), count),
                                  "findAffiliated/party", body)
        return data.get("suggestions") or []
