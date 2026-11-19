"""Разбор XML-извещений 44-ФЗ (схемы 2020 года: epNotificationEF2020, EZK2020, EOK2020...).

Разбор не завязан на пространства имён и точную вложенность: ищем элементы по
локальным именам. Так он переживёт мелкие изменения схем ЕИС, а поля, которых
в документе нет, просто остаются пустыми.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime


@dataclass
class Notice:
    purchase_number: str
    doc_type: str = ""              # epNotificationEF2020 и т.п.
    law: str = "44-ФЗ"
    subject: str = ""
    url: str = ""
    placing_way: str = ""           # электронный аукцион, запрос котировок...
    etp: str = ""
    customer: str = ""
    customer_inn: str = ""
    region: str = ""                # код региона заказчика (из запроса к ЕИС)
    published_at: datetime | None = None
    ends_at: datetime | None = None   # окончание подачи заявок
    max_price: float | None = None
    application_guarantee: float | None = None   # обеспечение заявки
    contract_guarantee: float | None = None      # обеспечение исполнения контракта
    warranty_guarantee: float | None = None      # обеспечение гарантийных обязательств
    okpd2: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("published_at", "ends_at"):
            d[k] = d[k].isoformat() if d[k] else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Notice":
        d = dict(d)
        for k in ("published_at", "ends_at"):
            d[k] = _dt(d.get(k))
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(el: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in el if _local(c.tag) == name]


def _find(el: ET.Element, path: str) -> ET.Element | None:
    """Путь из локальных имён через '/'; '//' в начале — искать первый шаг на любой глубине."""
    deep = path.startswith("//")
    steps = path.strip("/").split("/")
    starts = [d for d in el.iter() if _local(d.tag) == steps[0]] if deep else _children(el, steps[0])
    for start in starts:
        cur: ET.Element | None = start
        for step in steps[1:]:
            nxt = _children(cur, step)
            cur = nxt[0] if nxt else None
            if cur is None:
                break
        if cur is not None:
            return cur
    return None


def _text(el: ET.Element, *paths: str) -> str:
    for p in paths:
        hit = _find(el, p)
        if hit is not None and (hit.text or "").strip():
            return hit.text.strip()
    return ""


def _money(s: str) -> float | None:
    s = (s or "").replace(" ", "").replace("\xa0", "").replace(",", ".")
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _dt(s) -> datetime | None:
    if not s:
        return None
    if isinstance(s, datetime):
        return s
    try:
        return datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _sum_amounts(root: ET.Element, block: str, price: float | None) -> float | None:
    """Сумма блока обеспечения по всем заказчикам (совместные закупки). Если суммы нет, берём % от НМЦК."""
    total, seen = 0.0, False
    for el in root.iter():
        if _local(el.tag) != block:
            continue
        amount = _money(_text(el, "amount"))
        if amount is None and price:
            part = _money(_text(el, "part"))
            amount = round(price * part / 100, 2) if part else None
        if amount is not None:
            total += amount
            seen = True
    return total if seen else None


def parse_notice(xml: bytes | str, region: str = "") -> Notice | None:
    """Извещение из XML. Для документов другого вида (отмены, протоколы...) возвращает None."""
    root = ET.fromstring(xml)
    # Документ бывает обёрнут в <export>: само извещение — первый дочерний элемент.
    if _local(root.tag) == "export" and len(root):
        root = root[0]
    doc_type = _local(root.tag)
    if not doc_type.lower().startswith(("epnotification", "fcsnotification")):
        return None
    number = _text(root, "commonInfo/purchaseNumber", "purchaseNumber")
    if not number:
        return None
    price = _money(_text(root, "//contractConditionsInfo/maxPriceInfo/maxPrice", "//maxPrice"))
    codes = []
    for el in root.iter():
        if _local(el.tag) == "OKPDCode" and (el.text or "").strip():
            code = el.text.strip()
            if code not in codes:
                codes.append(code)
    return Notice(
        purchase_number=number,
        doc_type=doc_type,
        subject=_text(root, "commonInfo/purchaseObjectInfo", "//purchaseObjectInfo"),
        url=_text(root, "commonInfo/href", "//href"),
        placing_way=_text(root, "commonInfo/placingWayInfo/name", "//placingWay/name"),
        etp=_text(root, "commonInfo/ETPInfo/name", "//ETP/name"),
        customer=_text(root, "purchaseResponsibleInfo/responsibleOrgInfo/fullName",
                       "//responsibleOrgInfo/fullName", "//customer/fullName"),
        customer_inn=_text(root, "purchaseResponsibleInfo/responsibleOrgInfo/INN", "//responsibleOrgInfo/INN"),
        region=region,
        published_at=_dt(_text(root, "commonInfo/publishDTInEIS", "//publishDTInEIS", "//docPublishDate")),
        ends_at=_dt(_text(root, "//procedureInfo/collectingInfo/endDT", "//collecting/endDate", "//endDT")),
        max_price=price,
        application_guarantee=_sum_amounts(root, "applicationGuarantee", price),
        contract_guarantee=_sum_amounts(root, "contractGuarantee", price),
        warranty_guarantee=_sum_amounts(root, "provisionWarranty", price),
        okpd2=codes,
    )


def eis_url(number: str) -> str:
    """Ссылка на поиск закупки в ЕИС, если в документе её нет."""
    n = re.sub(r"\D", "", number)
    return f"https://zakupki.gov.ru/epz/order/extendedsearch/results.html?searchString={n}"
