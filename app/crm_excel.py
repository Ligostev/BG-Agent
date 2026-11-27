"""Выгрузка клиентов и сделок CRM в Excel и загрузка списка клиентов из Excel/CSV."""

from __future__ import annotations

import csv
import io
import re

from .crm_tasks import msk

CLIENT_COLUMNS = {
    "inn": ["инн"],
    "title": ["наименование", "название", "компания", "организация", "клиент"],
    "source": ["источник", "откуда"],
    "contact": ["контакт", "контактное лицо", "фио", "имя"],
    "position": ["должность"],
    "phone": ["телефон", "тел"],
    "email": ["email", "e-mail", "почта", "эл. почта"],
    "notes": ["комментарий", "заметки", "примечание"],
}


def _norm(h) -> str:
    return re.sub(r"\s+", " ", str(h or "").replace("﻿", "")).strip().lower()


def map_columns(headers: list) -> dict[str, int]:
    out: dict[str, int] = {}
    normed = [_norm(h) for h in headers]
    for exact in (True, False):
        for field, names in CLIENT_COLUMNS.items():
            if field in out:
                continue
            for i, h in enumerate(normed):
                if i in out.values() or not h:
                    continue
                if any(h == n if exact else n in h for n in names):
                    out[field] = i
                    break
    return out


def _rows(data: bytes, filename: str) -> list[list]:
    if filename.lower().endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        ws = load_workbook(io.BytesIO(data), read_only=True, data_only=True).active
        return [list(r) for r in ws.iter_rows(values_only=True)]
    text = data.decode("utf-8-sig", errors="replace")
    delim = ";" if text[:4096].count(";") > text[:4096].count(",") else ","
    return list(csv.reader(io.StringIO(text), delimiter=delim))


def load_clients(data: bytes, filename: str) -> list[dict]:
    """Строки с ИНН или названием. Строку заголовков ищем среди первых 20."""
    rows = _rows(data, filename)
    head = rows[:20]
    start = next((i for i, r in enumerate(head) if "inn" in map_columns(r)), None)
    if start is None:
        start = next((i for i, r in enumerate(head) if len(map_columns(r)) >= 2 and "title" in map_columns(r)), None)
    if start is None:
        raise ValueError("Не нашли колонки «ИНН» или «Наименование»")
    cols = map_columns(rows[start])
    out = []
    for r in rows[start + 1:]:
        get = lambda f: (str(r[cols[f]]).strip() if f in cols and cols[f] < len(r)
                         and r[cols[f]] is not None else "")
        inn = re.sub(r"\D", "", get("inn"))
        if len(inn) in (9, 11):     # Excel съел ведущий ноль
            inn = "0" + inn
        item = {f: get(f) for f in CLIENT_COLUMNS}
        item["inn"] = inn if len(inn) in (10, 12) else ""
        item["bad_inn"] = bool(inn) and not item["inn"]
        if item["inn"] or item["title"]:
            out.append(item)
    return out


def _book(headers: list[str], rows: list[list]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for r in rows:
        ws.append(r)
    for i, h in enumerate(headers, 1):
        width = max([len(str(h))] + [len(str(r[i - 1] or "")) for r in rows[:200]])
        ws.column_dimensions[ws.cell(1, i).column_letter].width = min(60, max(10, width + 2))
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_clients(clients, title) -> bytes:
    rows = []
    for c in clients:
        contact = c.contacts[0] if c.contacts else None
        rows.append([title(c), c.inn or "", c.ogrn or "", c.source or "",
                     c.manager.email if c.manager else "", c.user.email if c.user else "",
                     contact.name if contact else "", contact.phone if contact else "",
                     contact.email if contact else "", len(c.deals), msk(c.created_at, "%d.%m.%Y")])
    return _book(["Наименование", "ИНН", "ОГРН", "Источник", "Ответственный", "Кабинет", "Контакт",
                  "Телефон", "Email", "Сделок", "В CRM с"], rows)


def export_deals(deals, title) -> bytes:
    rows = [[d.id, title(d.client), d.client.inn or "", d.product, d.stage_title, d.amount, d.commission,
             d.purchase_number or "", ", ".join(f"{b.bank}: {b.status_title}" for b in d.banks),
             d.expires_at.strftime("%d.%m.%Y") if d.expires_at else "", msk(d.created_at, "%d.%m.%Y"),
             msk(d.closed_at, "%d.%m.%Y")] for d in deals]
    return _book(["№", "Клиент", "ИНН", "Продукт", "Этап", "Сумма гарантии, ₽", "Комиссия, ₽", "№ закупки",
                  "Банки", "Действует до", "Создана", "Закрыта"], rows)
