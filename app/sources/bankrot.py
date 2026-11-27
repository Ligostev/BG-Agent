"""Банкротство по ЕФРСБ: сам контрагент, намерения кредиторов, связанные лица из DaData и карточки ФНС."""

from __future__ import annotations

import re

from bankrot import FedresursClient, Party, check_bankruptcy

from . import SourceContext, SourceData
from .parties import related_parties, subject


class BankruptcySource:
    name = "bankrot"
    title = "Банкротство (ЕФРСБ)"

    def __init__(self, client: FedresursClient | None = None, max_related: int = 30):
        self.client = client or FedresursClient()
        self.max_related = max_related

    async def fetch(self, ctx: SourceContext) -> SourceData:
        identity = subject(ctx)
        query = re.sub(r"\D", "", ctx.query)
        ident = identity.get("inn") or query
        subj = Party(inn=ident, name=identity.get("title") or ident)
        exclude = {ident, identity.get("ogrn", ""), query}
        related = related_parties(ctx, exclude=exclude, limit=self.max_related)

        report = await check_bankruptcy(self.client, subj, related)
        out_identity = {}
        if not identity and report.subject.records:  # физлицо, которого нет ни в DaData, ни в «Прозрачном бизнесе»
            rec = report.subject.records[0]
            out_identity = {"title": rec.name, **({"inn": rec.inn} if rec.inn else {})}
        return SourceData(found=True, summary=report.summary(), data=report.to_dict(),
                          identity=out_identity)

    async def aclose(self) -> None:
        await self.client.aclose()
