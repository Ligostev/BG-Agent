"""Реестр юрлиц, привлечённых по ст. 19.28 КоАП (файл ЕИС): контрагент и связанные компании."""

from __future__ import annotations

import re

from bankrot import extract_parties
from koap1928 import Koap1928Client, check_koap1928

from . import SourceContext, SourceData


class Koap1928Source:
    name = "koap1928"
    title = "Реестр 19.28 КоАП (незаконное вознаграждение)"

    def __init__(self, client: Koap1928Client | None = None, max_related: int = 30):
        self.client = client or Koap1928Client()
        self.max_related = max_related

    async def fetch(self, ctx: SourceContext) -> SourceData:
        fns = ctx.previous.get("fns")
        identity = fns.identity if fns and fns.found else {}
        query = re.sub(r"\D", "", ctx.query)
        inn = identity.get("inn") or (query if len(query) in (10, 12) else "")
        ogrn = identity.get("ogrn") or (query if len(query) in (13, 15) else None)
        related = []
        if fns and fns.found and isinstance(fns.data, dict):
            card = {"hit": fns.data.get("hit"), "card": fns.data.get("card")}
            parties = extract_parties(card, exclude={inn, ogrn or "", query}, limit=self.max_related)
            # Ответственность по 19.28 несут юрлица, физлиц в реестре нет.
            related = [(p.inn, p.name, p.role) for p in parties if p.kind == "company"]

        registry = await self.client.registry()
        report = check_koap1928(registry, inn=inn, ogrn=ogrn, name=identity.get("title"),
                                related=related, stale_reason=self.client.stale_reason)
        out_identity = {}
        if not identity and report.subject.records:  # компании нет в «Прозрачном бизнесе»
            rec = report.subject.records[0]
            out_identity = {k: v for k, v in (("title", rec.name), ("inn", rec.inn),
                                              ("ogrn", rec.ogrn)) if v}
        return SourceData(found=True, summary=report.summary(), data=report.to_dict(),
                          identity=out_identity)

    async def aclose(self) -> None:
        await self.client.aclose()
