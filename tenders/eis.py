"""Сервис отдачи документов ЕИС для физлиц и ИП (getDocsIP).

Анонимный ФТП ЕИС закрыт с 1 января 2025 года. Документы теперь выдаёт SOAP-сервис
по токену, который пользователь получает на zakupki.gov.ru/pmd/auth/welcome через
Госуслуги («потребитель машиночитаемых данных», тип «Физическое лицо, ИП»).

Обмен:
1. POST getDocsByOrgRegionRequest (регион заказчика + вид документа + дата) ->
   в ответе ссылка dataInfo/archiveUrl (или признак, что документов нет);
2. GET archiveUrl с тем же токеном в заголовке individualPerson_token -> zip с XML.

Порядок элементов в запросе проверяется по XSD, менять его нельзя. Архив готовится
не сразу, поэтому скачивание повторяется. Число запросов в час ограничено, поэтому
клиент делает не больше одного запроса в min_interval секунд.

Сервис открывается только с российских IP.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime
from typing import AsyncIterator
from xml.sax.saxutils import escape

import httpx

from .notice import Notice, _find, _local, parse_notice

URL = "https://int44.zakupki.gov.ru/eis-integration/services/getDocsIP"
NS = "http://zakupki.gov.ru/fz44/get-docs-ip/ws"

# Виды извещений по законам: (подсистема ЕИС, виды документов).
# Элемент запроса для всех подсистем — documentType44 (так делают и другие клиенты сервиса).
DOC_TYPES: dict[str, tuple[str, tuple[str, ...]]] = {
    # электронный аукцион, запрос котировок, открытый конкурс
    "44-ФЗ": ("PRIZ", ("epNotificationEF2020", "epNotificationEZK2020", "epNotificationEOK2020")),
    "223-ФЗ": ("RI223", ("purchaseNotice", "purchaseNoticeOK", "purchaseNoticeOA", "purchaseNoticeAE",
                         "purchaseNoticeAE94FZ", "purchaseNoticeAESMBO", "purchaseNoticeZPESMBO")),
    # Капремонт по ПП РФ 615 (185-ФЗ). Название вида документа не сверено с живой ЕИС:
    # если сервис его не примет, это будет в журнале, остальные законы грузятся как обычно.
    "615-ПП": ("PRIZ", ("pprf615NotificationEF",)),
}
LAWS = tuple(DOC_TYPES)
NOTICE_TYPES = DOC_TYPES["44-ФЗ"][1]


def doc_types_from_env(raw: str) -> dict[str, tuple[str, tuple[str, ...]]]:
    """Переопределение видов документов, например
    EIS_DOC_TYPES="615-ПП=PRIZ:pprf615NotificationEF2020;223-ФЗ=RI223:purchaseNotice,purchaseNoticeAE"."""
    out = dict(DOC_TYPES)
    for part in filter(None, (x.strip() for x in (raw or "").split(";"))):
        law, _, rest = part.partition("=")
        subsystem, _, types = rest.partition(":")
        if law.strip() and subsystem.strip() and types.strip():
            out[law.strip()] = (subsystem.strip(), tuple(t.strip() for t in types.split(",") if t.strip()))
    return out


log = logging.getLogger(__name__)


class EisError(Exception):
    """Сервис ЕИС ответил ошибкой (неверный токен, лимит запросов, ошибка схемы...)."""


def build_region_request(region: str, doc_type: str, day: date, *, request_id: str | None = None,
                         now: datetime | None = None, subsystem: str = "PRIZ") -> str:
    now = now or datetime.now()
    return f"""<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ws="{NS}">
<soapenv:Header><individualPerson_token>{{token}}</individualPerson_token></soapenv:Header>
<soapenv:Body>
<ws:getDocsByOrgRegionRequest>
<index><id>{request_id or uuid.uuid4()}</id><createDateTime>{now.strftime("%Y-%m-%dT%H:%M:%S")}</createDateTime><mode>PROD</mode></index>
<selectionParams>
<orgRegion>{escape(region)}</orgRegion>
<subsystemType>{escape(subsystem)}</subsystemType>
<documentType44>{escape(doc_type)}</documentType44>
<periodInfo><exactDate>{day.isoformat()}</exactDate></periodInfo>
</selectionParams>
</ws:getDocsByOrgRegionRequest>
</soapenv:Body>
</soapenv:Envelope>"""


def parse_response(xml: bytes) -> list[str]:
    """Ссылки на архивы из ответа. Пустой список — документов за этот день нет."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise EisError(f"ЕИС вернула не XML: {xml[:200]!r}") from e
    fault = _find(root, "//Fault/faultstring")
    if fault is not None:
        raise EisError(fault.text or "SOAP Fault")
    urls = [el.text.strip() for el in root.iter() if _local(el.tag) == "archiveUrl" and (el.text or "").strip()]
    if urls:
        return urls
    if any(_local(el.tag) == "noData" for el in root.iter()):
        return []
    err = _find(root, "//errorInfo")
    if err is not None:
        raise EisError("".join(err.itertext()).strip() or "ошибка ЕИС")
    return []


def iter_archive(data: bytes) -> list[tuple[str, bytes]]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return [(n, zf.read(n)) for n in zf.namelist() if n.lower().endswith(".xml")]


class EisClient:
    def __init__(self, token: str, *, url: str = URL, min_interval: float = 2.0,
                 download_tries: int = 6, download_delay: float = 10.0,
                 http: httpx.AsyncClient | None = None,
                 doc_types: dict[str, tuple[str, tuple[str, ...]]] | None = None):
        if not token:
            raise EisError("Не задан токен ЕИС (переменная EIS_TOKEN)")
        self.token = token
        self.url = url
        self.min_interval = min_interval
        self.download_tries = download_tries
        self.download_delay = download_delay
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(60, read=180))
        self.doc_types = doc_types or doc_types_from_env(os.environ.get("EIS_DOC_TYPES", ""))
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def _throttle(self) -> None:
        async with self._lock:
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    async def archive_urls(self, region: str, doc_type: str, day: date, subsystem: str = "PRIZ") -> list[str]:
        await self._throttle()
        body = build_region_request(region, doc_type, day, subsystem=subsystem).replace("{token}", escape(self.token))
        r = await self.http.post(self.url, content=body.encode(),
                                 headers={"Content-Type": "text/xml; charset=utf-8"})
        if r.status_code >= 500 and b"Fault" not in r.content:
            raise EisError(f"ЕИС: HTTP {r.status_code}")
        return parse_response(r.content)

    async def download(self, archive_url: str) -> bytes:
        """Архив готовится не сразу: пока его нет, повторяем."""
        for attempt in range(self.download_tries):
            await self._throttle()
            r = await self.http.get(archive_url, headers={"individualPerson_token": self.token})
            if r.status_code == 200 and r.content[:2] == b"PK":
                return r.content
            if attempt + 1 < self.download_tries:
                await asyncio.sleep(self.download_delay)
        raise EisError(f"Архив не скачался: HTTP {r.status_code}, {r.content[:200]!r}")

    async def notices(self, region: str, day: date, laws: tuple[str, ...] = LAWS) -> AsyncIterator[Notice]:
        """Все извещения заказчиков региона, опубликованные в этот день, по выбранным законам.

        Ошибка по одному виду документа (например, неверное название) не мешает остальным.
        """
        for law in laws:
            subsystem, doc_types = self.doc_types.get(law, ("", ()))
            for doc_type in doc_types:
                try:
                    urls = await self.archive_urls(region, doc_type, day, subsystem)
                except EisError as e:
                    if "токен" in str(e).lower() or "token" in str(e).lower():
                        raise
                    log.error("ЕИС %s %s, регион %s: %s", law, doc_type, region, e)
                    continue
                for archive_url in urls:
                    for _, xml in iter_archive(await self.download(archive_url)):
                        try:
                            notice = parse_notice(xml, region=region)
                        except ET.ParseError:
                            continue
                        if notice is not None:
                            yield notice

    async def aclose(self) -> None:
        await self.http.aclose()
