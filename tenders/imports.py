"""Разбор Excel/CSV-выгрузок из поиска ЕИС (реестр контрактов, результаты поиска закупок).

Сопоставление колонок по синонимам взято из lead-parser (leads/tenders/core.py).
Здесь выгрузка нужна, чтобы собрать профиль клиента по его прошлым контрактам:
в ЕИС «Реестр контрактов» -> фильтр по ИНН поставщика -> «Выгрузить».
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime

COLUMNS = {
    "reg_number": ["реестровый номер контракта", "номер реестровой записи", "реестровый номер",
                   "номер контракта", "номер извещения", "номер закупки"],
    "customer": ["заказчик", "наименование заказчика"],
    "supplier": ["поставщик", "победитель", "наименование поставщика", "участник", "подрядчик"],
    "inn": ["инн поставщика", "инн победителя", "инн участника", "инн"],
    "price": ["цена контракта", "сумма контракта", "цена", "начальная (максимальная) цена", "нмцк"],
    "date": ["дата заключения", "дата подписания", "дата протокола", "дата подведения итогов",
             "дата размещения", "дата публикации", "дата"],
    "okpd2": ["окпд2", "окпд 2", "код окпд", "окпд"],
    "subject": ["объект закупки", "предмет контракта", "наименование объекта", "предмет"],
    "region": ["регион", "субъект рф", "место поставки"],
}


@dataclass
class Contract:
    reg_number: str = ""
    customer: str = ""
    supplier: str = ""
    inn: str = ""
    price: float | None = None
    date: str = ""
    okpd2: str = ""
    subject: str = ""
    region: str = ""


def _norm(h: str) -> str:
    return re.sub(r"\s+", " ", (h or "").replace("﻿", "")).strip().lower()


def map_headers(headers: list[str]) -> dict[str, str]:
    """Сопоставляет колонки файла полям. Сначала точные совпадения, потом по вхождению."""
    normed = {_norm(h): h for h in headers if h}
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for exact in (True, False):
        for fld, synonyms in COLUMNS.items():
            if fld in mapping:
                continue
            for syn in synonyms:
                hit = next((orig for n, orig in normed.items() if orig not in used
                            and (n == syn if exact else syn in n)), None)
                if hit:
                    mapping[fld] = hit
                    used.add(hit)
                    break
    return mapping


def parse_money(v) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^\d,.\-]", "", str(v).replace("\xa0", ""))
    if s.count(",") == 1 and s.count(".") <= 1 and s.rfind(",") > s.rfind("."):
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def parse_date(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v or "").strip()[:10]
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def read_rows(data: bytes, filename: str) -> list[dict]:
    if filename.lower().endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        ws = load_workbook(io.BytesIO(data), read_only=True, data_only=True).active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return []
        # В выгрузках ЕИС над таблицей бывают служебные строки: ищем строку заголовков.
        start = next((i for i, r in enumerate(rows[:20])
                      if len(map_headers([str(c or "") for c in r])) >= 3), 0)
        headers = [str(c or "") for c in rows[start]]
        return [dict(zip(headers, r)) for r in rows[start + 1:] if any(r)]
    text = data.decode("utf-8-sig", errors="replace")
    delim = ";" if text[:4096].count(";") > text[:4096].count(",") else ","
    return list(csv.DictReader(io.StringIO(text), delimiter=delim))


def load_contracts(data: bytes, filename: str) -> list[Contract]:
    rows = read_rows(data, filename)
    if not rows:
        return []
    m = map_headers(list(rows[0].keys()))
    if "okpd2" not in m and "price" not in m:
        raise ValueError("В файле нет колонок ОКПД2 и цены контракта. "
                         f"Колонки: {', '.join(str(k) for k in rows[0].keys())}")
    out = []
    for r in rows:
        get = lambda f: r.get(m[f]) if f in m else None
        d = parse_date(get("date"))
        out.append(Contract(
            reg_number=str(get("reg_number") or "").strip().lstrip("№ "),
            customer=str(get("customer") or "").strip(),
            supplier=str(get("supplier") or "").strip(),
            inn=re.sub(r"\D", "", str(get("inn") or "")),
            price=parse_money(get("price")),
            date=d.isoformat() if d else "",
            okpd2=str(get("okpd2") or "").strip(),
            subject=str(get("subject") or "").strip(),
            region=str(get("region") or "").strip(),
        ))
    return out
