"""DaData: основной источник карточки юрлица или ИП (официальный API, без капчи)."""

from __future__ import annotations

import re

from dadata import DaDataClient, parse_party
from pbnalog.validators import classify

from . import SourceContext, SourceData


class DaDataSource:
    name = "dadata"
    title = "Карточка компании (DaData)"

    def __init__(self, client: DaDataClient | None = None):
        self.client = client or DaDataClient()

    async def fetch(self, ctx: SourceContext) -> SourceData:
        query = re.sub(r"\D", "", ctx.query)
        id_type = classify(query)
        if id_type is None:
            return SourceData(found=False)
        # По ИНН физлица ищем только ИП; по ОГРН/ОГРНИП тип и так однозначен.
        kind = {"inn_ul": "LEGAL", "inn_fl": "INDIVIDUAL"}.get(id_type)
        suggestion = await self.client.find_party(query, kind=kind)
        if suggestion is None:
            return SourceData(found=False)
        card = parse_party(suggestion)
        data = {"kind": card.kind, "status": card.status, "flags": card.flags,
                "parties": card.parties, "suggestion": suggestion}
        if card.flags:
            data["flagged"] = True
            data["verdict"] = "Внимание: " + "; ".join(card.flags)
        return SourceData(found=True, summary=card.summary, data=data, identity=card.identity)

    async def aclose(self) -> None:
        await self.client.aclose()
