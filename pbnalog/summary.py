"""Ключевые показатели из результата PbNalogClient.lookup() для бота и кабинета."""

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


def kind_title(kind: str) -> str:
    return "Юридическое лицо" if kind == "ul" else "Индивидуальный предприниматель"


def summarize(result: dict) -> list[tuple[str, str]]:
    """[(подпись, значение), ...] только для заполненных показателей."""
    merged = {**result.get("card", {}), **result["hit"]}
    rows = []
    for label, keys in FIELDS:
        value = pick(merged, keys)
        if value is not None:
            rows.append((label, str(value)))
    return rows
