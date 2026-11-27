"""Банкротство по ЕФРСБ: сам контрагент, намерения кредиторов, связанные лица из карточки ФНС."""

from __future__ import annotations

import re

from bankrot import FedresursClient, Party, check_bankruptcy, extract_parties

from . import SourceContext, SourceData


class BankruptcySource:
    name = "bankrot"
    title = "Банкротство (ЕФРСБ)"

    def __init__(self, client: FedresursClient | None = None, max_related: int = 30):
        self.client = client or FedresursClient()
        self.max_related = max_related

    async def fetch(self, ctx: SourceContext) -> SourceData:
        fns = ctx.previous.get("fns")
        identity = fns.identity if fns and fns.found else {}
        ident = identity.get("inn") or re.sub(r"\D", "", ctx.query)
        subject = Party(inn=ident, name=identity.get("title") or ident)
        related = []
        if fns and fns.found and isinstance(fns.data, dict):
            card = {"hit": fns.data.get("hit"), "card": fns.data.get("card")}
            exclude = {ident, identity.get("ogrn", ""), re.sub(r"\D", "", ctx.query)}
            related = extract_parties(card, exclude=exclude, limit=self.max_related)

        report = await check_bankruptcy(self.client, subject, related)
        out_identity = {}
        if not identity and report.subject.records:  # физлицо, которого нет в «Прозрачном бизнесе»
            rec = report.subject.records[0]
            out_identity = {"title": rec.name, **({"inn": rec.inn} if rec.inn else {})}
        return SourceData(found=True, summary=report.summary(), data=report.to_dict(),
                          identity=out_identity)

    async def aclose(self) -> None:
        await self.client.aclose()
