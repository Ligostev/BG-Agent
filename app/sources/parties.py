"""Кто проверяется и с кем он связан — общее для источников-проверок.

Сначала берутся сведения DaData (официальный API, у учредителей и руководителей
есть ИНН и доли), затем добавляется то, что нашлось в карточке pb.nalog.ru.
На бесплатном тарифе DaData учредителей нет, поэтому второй источник важен.
"""

from __future__ import annotations

import re

from bankrot import Party, extract_parties

from . import SourceContext


def subject(ctx: SourceContext) -> dict[str, str]:
    """Название, ИНН, ОГРН и тип контрагента: DaData важнее pb.nalog.ru."""
    out: dict[str, str] = {}
    for name in ("fns", "dadata"):
        data = ctx.previous.get(name)
        if data and data.found:
            out.update(data.identity)
    return out


def found_any(ctx: SourceContext) -> bool:
    return any(ctx.previous.get(n) and ctx.previous[n].found for n in ("dadata", "fns"))


def related_parties(ctx: SourceContext, exclude: set[str] | None = None, limit: int = 30,
                    with_inn: bool = True) -> list[Party]:
    """Учредители, руководители, правопредшественники и т. п.

    with_inn=False оставляет и лиц без ИНН (руководитель из базовой карточки
    DaData) — для проверок по ФИО, например реестра дисквалифицированных.
    """
    exclude = {x for x in (exclude or set()) if x}
    found: dict[str, Party] = {}
    order: list[str] = []

    def add(key: str, party: Party) -> None:
        if key in found:
            for role in party.roles:
                if role not in found[key].roles:
                    found[key].roles.append(role)
            return
        if len(found) < limit:
            found[key] = party
            order.append(key)

    dd = ctx.previous.get("dadata")
    if dd and dd.found and isinstance(dd.data, dict):
        for p in dd.data.get("parties") or []:
            inn = re.sub(r"\D", "", p.get("inn") or "")
            if inn in exclude or (with_inn and not inn):
                continue
            key = inn or "fio:" + (p.get("name") or "").lower()
            add(key, Party(inn=inn, name=p.get("name") or inn, roles=p["role"].split(", ")))

    fns = ctx.previous.get("fns")
    if fns and fns.found and isinstance(fns.data, dict):
        card = {"hit": fns.data.get("hit"), "card": fns.data.get("card")}
        for p in extract_parties(card, exclude=exclude, limit=limit):
            add(p.inn, p)

    return [found[k] for k in order]
