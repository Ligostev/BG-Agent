"""Открытые данные ФНС: дисквалифицированные лица и налоговые правонарушения."""

from __future__ import annotations

import asyncio
import os
import re
from datetime import date
from pathlib import Path

from bankrot import extract_parties
from fnsdata import DisqualifiedRegistry, OpenDataset, TaxOffenceIndex, build_taxoffence_index

from . import SourceContext, SourceData

DISQUALIFIED = "7707329152-registerdisqualified"
TAXOFFENCE = "7707329152-taxoffence"


def default_dir() -> str:
    return os.environ.get("FNS_OPENDATA_DIR", "./data/fns-opendata")


def _subject(ctx: SourceContext) -> tuple[dict, str, dict | None]:
    fns = ctx.previous.get("fns")
    identity = fns.identity if fns and fns.found else {}
    query = re.sub(r"\D", "", ctx.query)
    inn = identity.get("inn") or (query if len(query) in (10, 12) else "")
    card = None
    if fns and fns.found and isinstance(fns.data, dict):
        card = {"hit": fns.data.get("hit"), "card": fns.data.get("card")}
    return identity, inn, card


def _stale(ds: OpenDataset) -> list[tuple[str, str]]:
    if ds.stale_reason:
        return [("Внимание", f"ФНС не ответила, использована прошлая копия набора ({ds.stale_reason})")]
    return []


class DisqualifiedSource:
    """Дисквалификация (ст. 3.11 КоАП): кто из руководителей и учредителей лишён права руководить."""
    name = "disqualified"
    title = "ФНС: дисквалифицированные лица"

    def __init__(self, dataset: OpenDataset | None = None, max_related: int = 30):
        self.dataset = dataset or OpenDataset(DISQUALIFIED, default_dir())
        self.max_related = max_related
        self._registry: DisqualifiedRegistry | None = None
        self._loaded: tuple[str, float] | None = None

    async def registry(self) -> DisqualifiedRegistry:
        path = await self.dataset.path()
        key = (str(path), path.stat().st_mtime)
        if self._registry is None or self._loaded != key:
            self._registry = await asyncio.to_thread(DisqualifiedRegistry.load, path,
                                                     self.dataset.source_url())
            self._loaded = key
        return self._registry

    async def fetch(self, ctx: SourceContext) -> SourceData:
        identity, inn, card = _subject(ctx)
        reg = await self.registry()
        today = date.today()
        people = []
        if card:
            people = [p for p in extract_parties(card, exclude={inn}, limit=self.max_related)
                      if p.kind == "person"]

        by_org = reg.by_org(inn) if inn else []
        matches = []  # (лицо из карточки, запись реестра)
        for p in people:
            for rec in reg.by_name(p.name):
                matches.append((p, rec))
        if len(inn) == 12:  # ИП: сам человек
            name = identity.get("title") or ""
            name = re.sub(r"^(ИП|Индивидуальный предприниматель)\s+", "", name, flags=re.I)
            matches += [(None, rec) for rec in reg.by_name(name)] if name else []

        active_heads = [(p, r) for p, r in matches
                        if r.active(today) and (p is None or "Руководитель" in p.roles)]
        if active_heads:
            verdict = "Руководитель (по ФИО) в реестре дисквалифицированных, срок не истёк"
        elif any(r.active(today) for _, r in matches) or any(r.active(today) for r in by_org):
            verdict = "Есть совпадения с действующей дисквалификацией"
        elif matches or by_org:
            verdict = "Были дисквалификации, сроки истекли"
        else:
            verdict = "В реестре дисквалифицированных не найдено"
        flagged = bool(active_heads)

        rows = [("Итог", verdict)]
        for r in by_org:
            rows.append((f"Дисквалифицирован из этой компании ({'действует' if r.active(today) else 'истёк'})",
                         r.describe()))
        for p, r in matches:
            who = f"{p.role}: {p.name}" if p else "Предприниматель"
            rows.append((f"{who} — совпадение по ФИО ({'действует' if r.active(today) else 'истёк'})",
                         r.describe() + ". Сверьте дату рождения, может быть однофамилец"))
        if people:
            rows.append(("Проверено лиц из карточки ФНС", str(len(people))))
        rows.append(("Реестр ФНС", f"{len(reg)} записей"))
        rows += _stale(self.dataset)
        data = {"flagged": flagged, "verdict": verdict,
                "by_org": [r.to_dict(today) for r in by_org],
                "by_name": [{"party": p.name if p else None, "roles": p.roles if p else [],
                             "record": r.to_dict(today)} for p, r in matches],
                "checked_people": [p.name for p in people], "registry_size": len(reg),
                "source": reg.source}
        return SourceData(found=True, summary=rows, data=data)

    async def aclose(self) -> None:
        await self.dataset.aclose()


class TaxOffenceSource:
    """Штрафы за налоговые правонарушения (НК РФ) по данным ФНС за прошлый год."""
    name = "taxoffence"
    title = "ФНС: налоговые правонарушения"

    def __init__(self, dataset: OpenDataset | None = None):
        self.dataset = dataset or OpenDataset(TAXOFFENCE, default_dir())
        self._build_lock = asyncio.Lock()

    async def index(self) -> TaxOffenceIndex:
        path = await self.dataset.path()
        index = TaxOffenceIndex(Path(path).with_suffix(".db"))
        async with self._build_lock:
            if not await asyncio.to_thread(index.is_current, path):
                await asyncio.to_thread(build_taxoffence_index, path, index.db_path)
        return index

    async def fetch(self, ctx: SourceContext) -> SourceData:
        _, inn, _ = _subject(ctx)
        if len(inn) != 10:
            return SourceData(found=False)  # набор только по организациям
        index = await self.index()
        found = await asyncio.to_thread(index.find, inn)
        total = sum(r.fine for r in found)
        when = max((r.doc_date or "" for r in found), default="")
        if found:
            verdict = f"Штрафы за налоговые правонарушения: {total:,.0f} ₽".replace(",", " ")
        else:
            verdict = "Налоговых правонарушений в сведениях ФНС нет"
        rows = [("Итог", verdict)]
        if when:
            rows.append(("Сведения на", when[:10]))
        rows += _stale(self.dataset)
        data = {"flagged": bool(found and total > 0), "verdict": verdict, "total_fine": total,
                "records": [r.__dict__ for r in found], "source": self.dataset.source_url()}
        return SourceData(found=True, summary=rows, data=data)

    async def aclose(self) -> None:
        await self.dataset.aclose()
