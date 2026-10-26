"""Текст ответа бота по результату PbNalogClient.lookup()."""

from html import escape
from typing import Any

# Названия полей в ответах pb.nalog.ru пока не сверены с живым сайтом,
# поэтому для каждого показателя перебираем несколько вариантов.
FIELDS = [
    ("Наименование", ("namec", "namep", "name", "name_ex")),
    ("ИНН", ("inn",)),
    ("ОГРН", ("ogrn",)),
    ("Статус", ("sulst_name", "sipst_name", "status")),
    ("Дата регистрации", ("dtreg", "dtogrn", "registration_date")),
    ("ОКВЭД", ("okved2name", "okved_name", "okved2", "okved")),
    ("Регион", ("regionname", "region")),
    ("Адрес", ("address", "adr", "addr")),
]


def pick(data: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        v = data.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def format_result(result: dict) -> str:
    kind = "Юридическое лицо" if result["kind"] == "ul" else "Индивидуальный предприниматель"
    merged = {**result.get("card", {}), **result["hit"]}
    lines = [f"<b>{escape(kind)}</b>"]
    for label, keys in FIELDS:
        value = pick(merged, keys)
        if value is not None:
            lines.append(f"{label}: {escape(str(value))}")
    lines.append("\nПолный ответ ФНС во вложении.")
    return "\n".join(lines)
