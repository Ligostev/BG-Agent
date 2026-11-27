"""Разбор ответа findById/party в карточку для кабинета и проверок.

Состав полей зависит от тарифа DaData: на бесплатном есть реквизиты, статус,
адрес, ОКВЭД и руководитель; численность и налоговый режим — с «Расширенного»;
учредители, все руководители с ИНН, финансы и лицензии — на «Максимальном».
Пустые поля в сводку не попадают.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

STATUS = {
    "ACTIVE": "Действующая",
    "LIQUIDATING": "Ликвидируется",
    "LIQUIDATED": "Ликвидирована",
    "BANKRUPT": "Банкротство",
    "REORGANIZING": "Реорганизация (присоединение к другому юрлицу)",
}
# Статусы, при которых работать с контрагентом нельзя или рискованно.
BAD_STATUS = {"LIQUIDATING", "LIQUIDATED", "BANKRUPT", "REORGANIZING"}

TAX_SYSTEM = {
    "ENVD": "ЕНВД", "ESHN": "ЕСХН", "SRP": "СРП", "USN": "УСН", "AUSN": "АУСН",
    "PSN": "Патент", "NPD": "НПД", "ENVD_ESHN": "ЕНВД + ЕСХН", "USN_ENVD": "УСН + ЕНВД",
    "ESHN_PSN": "ЕСХН + Патент", "USN_PSN": "УСН + Патент",
}
SMB = {"MICRO": "микропредприятие", "SMALL": "малое", "MEDIUM": "среднее"}


@dataclass
class Card:
    kind: str                       # "ul" или "ip", как в pbnalog
    title: str
    inn: str | None = None
    ogrn: str | None = None
    status: str | None = None       # код DaData: ACTIVE, LIQUIDATED, ...
    summary: list[tuple[str, str]] = field(default_factory=list)
    # Связанные лица: {"inn", "name", "role", "kind": company|person, "share", "post"}.
    parties: list[dict] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)   # тревожные признаки

    @property
    def identity(self) -> dict[str, str]:
        out = {"kind": self.kind, "title": self.title}
        if self.inn:
            out["inn"] = self.inn
        if self.ogrn:
            out["ogrn"] = self.ogrn
        return out


def _date(ms: Any) -> str | None:
    if ms in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%d.%m.%Y")
    except (TypeError, ValueError, OverflowError, OSError):
        return str(ms)


def _money(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return f"{float(value):,.0f} ₽".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def fio_text(fio: Any) -> str | None:
    if isinstance(fio, str):
        return fio.strip() or None
    if isinstance(fio, dict):
        parts = [fio.get(k) for k in ("surname", "name", "patronymic")]
        text = " ".join(str(p).strip() for p in parts if p)
        return text or (fio.get("source") or None)
    return None


def _share(share: Any) -> str | None:
    if not isinstance(share, dict):
        return None
    kind, value = share.get("type"), share.get("value")
    if kind == "PERCENT" and value is not None:
        return f"{value:g}%" if isinstance(value, (int, float)) else f"{value}%"
    if kind == "FRACTION" and share.get("numerator") and share.get("denominator"):
        return f"{share['numerator']}/{share['denominator']}"
    if value is not None:
        return str(value)
    return None


def _okved(d: dict) -> str | None:
    code = d.get("okved")
    if not code:
        return None
    for item in d.get("okveds") or []:
        if isinstance(item, dict) and item.get("code") == code and item.get("name"):
            return f"{code} {item['name']}"
    return str(code)


def _parties(d: dict) -> list[dict]:
    out: dict[str, dict] = {}
    nameless: list[dict] = []

    def add(inn: Any, name: str | None, role: str, kind: str, share: str | None = None,
            post: str | None = None) -> None:
        inn = str(inn).strip() if inn else ""
        if not name and not inn:
            return
        if not inn:
            if not any(p["name"] == name for p in nameless):
                nameless.append({"inn": "", "name": name, "role": role, "kind": kind,
                                 "share": share, "post": post})
            return
        p = out.get(inn)
        if p is None:
            out[inn] = {"inn": inn, "name": name or inn, "role": role,
                        "kind": "company" if len(inn) == 10 else "person", "share": share,
                        "post": post}
        else:
            if role not in p["role"].split(", "):
                p["role"] += f", {role}"
            p["share"] = p["share"] or share
            p["post"] = p["post"] or post

    for m in d.get("managers") or []:
        if not isinstance(m, dict):
            continue
        legal = m.get("type") == "LEGAL"
        name = m.get("name") if legal else fio_text(m.get("fio")) or m.get("name")
        # Роль «Руководитель» без должности: по ней другие проверки узнают руководителя.
        role = "Управляющая компания" if legal else "Руководитель"
        add(m.get("inn"), name, role, "company" if legal else "person", post=m.get("post"))
    for f in d.get("founders") or []:
        if not isinstance(f, dict):
            continue
        legal = f.get("type") == "LEGAL"
        name = f.get("name") if legal else fio_text(f.get("fio")) or f.get("name")
        add(f.get("inn"), name, "Учредитель", "company" if legal else "person", _share(f.get("share")))
    for key, role in (("predecessors", "Правопредшественник"), ("successors", "Правопреемник")):
        for c in d.get(key) or []:
            if isinstance(c, dict):
                add(c.get("inn"), c.get("name"), role, "company")

    # Руководитель из базовых полей (без ИНН) — если полного списка руководителей нет.
    mgmt = d.get("management") or {}
    name = mgmt.get("name")
    if name and not any(p["name"].lower() == name.lower() for p in out.values()):
        add(None, name, "Руководитель", "person", post=mgmt.get("post"))
    return list(out.values()) + nameless


def parse_party(suggestion: dict) -> Card:
    d = suggestion.get("data") or {}
    kind = "ip" if d.get("type") == "INDIVIDUAL" else "ul"
    names = d.get("name") or {}
    title = (names.get("short_with_opf") or suggestion.get("value") or names.get("full_with_opf")
             or fio_text(d.get("fio")) or d.get("inn") or "")
    state = d.get("state") or {}
    status = state.get("status")

    rows: list[tuple[str, str]] = []
    flags: list[str] = []

    def row(label: str, value: Any) -> None:
        if value not in (None, "", [], {}):
            rows.append((label, str(value)))

    row("Тип", "Юридическое лицо" if kind == "ul" else "Индивидуальный предприниматель")
    row("Наименование", title)
    full = names.get("full_with_opf")
    if full and full != title:
        row("Полное наименование", full)
    row("ИНН", d.get("inn"))
    row("КПП", d.get("kpp"))
    ogrn_date = _date(d.get("ogrn_date"))
    row("ОГРН" if kind == "ul" else "ОГРНИП",
        f"{d['ogrn']} от {ogrn_date}" if d.get("ogrn") and ogrn_date else d.get("ogrn"))
    if status:
        row("Статус", STATUS.get(status, status))
        if status in BAD_STATUS:
            flags.append(STATUS.get(status, status))
    row("Дата регистрации", _date(state.get("registration_date")))
    row("Дата ликвидации", _date(state.get("liquidation_date")))
    row("Сведения актуальны на", _date(state.get("actuality_date")))

    mgmt = d.get("management") or {}
    if mgmt.get("name"):
        row("Руководитель", f"{mgmt['name']}, {mgmt['post'].lower()}" if mgmt.get("post")
            else mgmt["name"])
    if mgmt.get("disqualified"):
        row("Дисквалификация", "В руководстве есть дисквалифицированные лица")
        flags.append("Дисквалифицированные лица в руководстве")
    if d.get("invalid"):
        row("Недостоверность", "ФНС отметила сведения как недостоверные")
        flags.append("Недостоверные сведения в ЕГРЮЛ")

    addr = d.get("address") or {}
    row("Адрес", addr.get("value") or (addr.get("data") or {}).get("source"))
    row("Регион", (addr.get("data") or {}).get("region_with_type"))
    row("ОКВЭД", _okved(d))
    opf = d.get("opf") or {}
    row("ОПФ", opf.get("full"))
    capital = d.get("capital") or {}
    row("Уставный капитал", _money(capital.get("value")))
    row("Среднесписочная численность", d.get("employee_count"))
    finance = d.get("finance") or {}
    tax = finance.get("tax_system")
    row("Налоговый режим", TAX_SYSTEM.get(tax, tax) if tax else None)
    year = finance.get("year")
    suffix = f" за {year}" if year else ""
    row("Доходы" + suffix, _money(finance.get("income")))
    row("Расходы" + suffix, _money(finance.get("expense")))
    row("Выручка" + suffix, _money(finance.get("revenue")))
    row("Недоимки по налогам" + suffix, _money(finance.get("debt")))
    row("Налоговые штрафы" + suffix, _money(finance.get("penalty")))
    if finance.get("debt"):
        flags.append("Недоимки по налогам")
    smb = ((d.get("documents") or {}).get("smb") or {})
    if smb.get("category"):
        row("Реестр МСП", SMB.get(smb["category"], smb["category"]))
    if d.get("branch_count"):
        row("Филиалов", d["branch_count"])
    licenses = d.get("licenses") or []
    if licenses:
        row("Лицензий", len(licenses))

    parties = _parties(d)
    founders = [p for p in parties if "Учредитель" in p["role"]]
    if founders:
        row("Учредители", "; ".join(
            p["name"] + (f" ({p['share']})" if p.get("share") else "") for p in founders[:10])
            + (f" и ещё {len(founders) - 10}" if len(founders) > 10 else ""))
    if flags:
        rows.insert(0, ("Внимание", "; ".join(flags)))

    return Card(kind=kind, title=title, inn=d.get("inn"), ogrn=d.get("ogrn"), status=status,
                summary=rows, parties=parties, flags=flags)
