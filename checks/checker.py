"""Сводная проверка компании или ИП по всем источникам.

Не зависит от Telegram: тот же отчёт пригодится веб-кабинету. Источники
опрашиваются по очереди (капча у каждого своя, и пользователь решает их
по одной). Ошибка одного источника не мешает остальным: в отчёте у источника
будет status="error" и текст ошибки.

Отчёт — обычный dict, его можно сразу отдавать в JSON:

    {
      "query": "7707083893", "kind": "ul" | "ip", "inn": ..., "name": ...,
      "sources": {
        "pbnalog": {"status": "ok", "data": {...}},
        "rnp":     {"status": "ok", "entries": [...]},
        "fssp":    {"status": "ok", "proceedings": [...], "by": "name", "note": ...},
      },
      "flags": ["В реестре недобросовестных поставщиков: 1 действующая запись", ...],
    }
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fssp import FsspClient, Proceeding
from pbnalog import NotFound, PbNalogClient, classify
from rnp import RnpClient

log = logging.getLogger(__name__)

NAME_KEYS = ("namec", "namep", "name", "name_ex")
LEGAL_FORMS = re.compile(
    r"\b(ООО|АО|ПАО|ЗАО|ОАО|НАО|ИП|ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ|"
    r"ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО|НЕПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО|"
    r"ЗАКРЫТОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО|ОТКРЫТОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО|АКЦИОНЕРНОЕ ОБЩЕСТВО|"
    r"ИНДИВИДУАЛЬНЫЙ ПРЕДПРИНИМАТЕЛЬ)\b")


def normalize_name(name: str) -> str:
    """Название без кавычек, знаков и организационно-правовой формы, в верхнем регистре."""
    s = re.sub(r"[^\w\s]", " ", name.upper().replace("Ё", "Е"))
    return " ".join(LEGAL_FORMS.sub(" ", s).split())


def _pick(data: dict, keys: tuple[str, ...]) -> str:
    for k in keys:
        if data.get(k):
            return str(data[k])
    return ""


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _error(e: Exception) -> dict:
    return {"status": "error", "error_type": type(e).__name__, "error": str(e)}


def make_flags(report: dict) -> list[str]:
    flags = []
    rnp = report["sources"].get("rnp", {})
    if rnp.get("status") == "ok":
        active = sum(e["active"] for e in rnp["entries"])
        if active:
            flags.append(f"В реестре недобросовестных поставщиков: {active} "
                         + _plural(active, "действующая запись", "действующие записи", "действующих записей"))
        elif rnp["entries"]:
            flags.append("Был в реестре недобросовестных поставщиков, сейчас исключён")
    fssp = report["sources"].get("fssp", {})
    if fssp.get("status") == "ok":
        active = [p for p in fssp["proceedings"] if p["active"]]
        if active:
            debt = sum(p["amount"] or 0 for p in active)
            text = f"Исполнительные производства: {len(active)} " + _plural(
                len(active), "действующее", "действующих", "действующих")
            if debt:
                text += ", остаток долга " + f"{debt:,.2f}".replace(",", " ") + " руб."
            flags.append(text)
    return flags


class CompanyChecker:
    def __init__(self, pb: PbNalogClient, rnp: RnpClient, fssp: FsspClient):
        self.pb, self.rnp, self.fssp = pb, rnp, fssp

    async def check(self, query: str) -> dict[str, Any]:
        q = re.sub(r"\D", "", query)
        id_type = classify(q)
        if id_type is None:
            raise ValueError(f"{query!r} не похоже на корректный ИНН или ОГРН")
        report: dict[str, Any] = {
            "query": q,
            "kind": "ul" if id_type in ("inn_ul", "ogrn") else "ip",
            "inn": q if id_type.startswith("inn") else "",
            "name": "",
            "sources": {},
        }
        src = report["sources"]

        try:
            card = await self.pb.lookup(q)
            src["pbnalog"] = {"status": "ok", "data": card}
            report["inn"] = report["inn"] or str(card["hit"].get("inn", ""))
            report["name"] = _pick(card["hit"], NAME_KEYS) or _pick(card.get("card", {}), NAME_KEYS)
        except NotFound:
            src["pbnalog"] = {"status": "not_found"}
        except Exception as e:  # noqa: BLE001 — один источник не должен ронять отчёт
            log.exception("pb.nalog.ru")
            src["pbnalog"] = _error(e)

        if report["inn"]:
            try:
                src["rnp"] = {"status": "ok",
                              "entries": [e.to_dict() for e in await self.rnp.check(report["inn"])]}
            except Exception as e:  # noqa: BLE001
                log.exception("РНП")
                src["rnp"] = _error(e)
        else:
            src["rnp"] = {"status": "skipped", "reason": "ИНН неизвестен: ФНС не вернула карточку"}

        if report["name"]:
            try:
                src["fssp"] = await self._fssp(report["kind"], report["name"])
            except Exception as e:  # noqa: BLE001
                log.exception("ФССП")
                src["fssp"] = _error(e)
        else:
            src["fssp"] = {"status": "skipped",
                           "reason": "ФССП ищет только по названию или ФИО, а их не удалось получить из ФНС"}

        report["flags"] = make_flags(report)
        return report

    async def _fssp(self, kind: str, name: str) -> dict:
        if kind == "ul":
            core = normalize_name(name)
            rows: list[Proceeding] = await self.fssp.search_legal(name)
            # Поиск по названию нечёткий: оставляем строки, где должник назван так же.
            rows = [r for r in rows if normalize_name(r.debtor_name) == core]
            note = "Поиск по наименованию: возможны совпадения с другими юрлицами с тем же названием."
        else:
            parts = normalize_name(name).split()
            last, first, patronymic = (parts + ["", "", ""])[:3]
            rows = await self.fssp.search_person(last, first, patronymic)
            note = "Поиск по ФИО без даты рождения: в списке могут быть тёзки."
        return {"status": "ok", "by": "name", "query": name, "note": note,
                "proceedings": [r.to_dict() for r in rows]}
