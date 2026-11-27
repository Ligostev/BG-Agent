"""Разбор XML-извещений ЕИС: 44-ФЗ (epNotificationEF2020, EZK2020, EOK2020...),
223-ФЗ (purchaseNotice, purchaseNoticeAE...) и закупок по ПП РФ 615 (pprf615...).

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
    okpd2_names: list[str] = field(default_factory=list)   # названия позиций: для поиска по словам

    @property
    def text(self) -> str:
        """Всё, по чему ищем ключевые слова."""
        return " ".join([self.subject, *self.okpd2_names])

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


def _unique_texts(root: ET.Element, parent: str, child: str) -> list[str]:
    """Тексты <child> внутри всех <parent> без повторов (коды и названия ОКПД2)."""
    out: list[str] = []
    for el in root.iter():
        if _local(el.tag) != parent:
            continue
        for c in el:
            if _local(c.tag) == child and (c.text or "").strip() and c.text.strip() not in out:
                out.append(c.text.strip())
    return out


def law_of(doc_type: str) -> str | None:
    t = doc_type.lower()
    if "615" in t:
        return "615-ПП"
    if t.startswith("purchasenotice"):
        return "223-ФЗ"
    if t.startswith(("epnotification", "fcsnotification")):
        return "44-ФЗ"
    return None


def _parse_44(root: ET.Element, doc_type: str, law: str, region: str) -> Notice | None:
    """44-ФЗ и закупки по ПП РФ 615 (у них общий формат извещений ЕИС)."""
    number = _text(root, "commonInfo/purchaseNumber", "purchaseNumber", "//purchaseNumber")
    if not number:
        return None
    price = _money(_text(root, "//contractConditionsInfo/maxPriceInfo/maxPrice", "//maxPrice"))
    names = [*_unique_texts(root, "OKPD2", "OKPDName"), *_unique_texts(root, "purchaseObject", "name")]
    return Notice(
        purchase_number=number,
        doc_type=doc_type,
        law=law,
        subject=_text(root, "commonInfo/purchaseObjectInfo", "//purchaseObjectInfo"),
        url=_text(root, "commonInfo/href", "//href"),
        placing_way=_text(root, "commonInfo/placingWayInfo/name", "//placingWay/name"),
        etp=_text(root, "commonInfo/ETPInfo/name", "//ETP/name"),
        customer=_text(root, "purchaseResponsibleInfo/responsibleOrgInfo/fullName",
                       "//responsibleOrgInfo/fullName", "//customer/fullName"),
        customer_inn=_text(root, "purchaseResponsibleInfo/responsibleOrgInfo/INN", "//responsibleOrgInfo/INN"),
        region=region,
        published_at=_dt(_text(root, "commonInfo/publishDTInEIS", "//publishDTInEIS", "//docPublishDate")),
        ends_at=_dt(_text(root, "//procedureInfo/collectingInfo/endDT", "//collecting/endDate", "//endDT",
                          "//endDate")),
        max_price=price,
        application_guarantee=_sum_amounts(root, "applicationGuarantee", price),
        contract_guarantee=_sum_amounts(root, "contractGuarantee", price),
        warranty_guarantee=_sum_amounts(root, "provisionWarranty", price),
        okpd2=_unique_texts(root, "OKPD2", "OKPDCode") or _unique_texts(root, "OKPD2", "code"),
        okpd2_names=list(dict.fromkeys(names)),
    )


def _parse_223(root: ET.Element, doc_type: str, region: str) -> Notice | None:
    """Извещения 223-ФЗ (purchaseNotice, purchaseNoticeAE, ...): данные в purchaseNotice*Data, цены по лотам."""
    data = next((el for el in root.iter() if _local(el.tag).startswith("purchaseNotice")
                 and _local(el.tag).endswith("Data")), root)
    number = _text(data, "registrationNumber", "//registrationNumber")
    if not number:
        return None
    sums = [_money(el.text or "") for el in data.iter() if _local(el.tag) == "initialSum"]
    sums = [x for x in sums if x is not None]
    names = [*_unique_texts(data, "okpd2", "name"), *_unique_texts(data, "lotData", "subject")]
    return Notice(
        purchase_number=number,
        doc_type=doc_type,
        law="223-ФЗ",
        subject=_text(data, "name", "//lotData/subject"),
        url=_text(data, "urlEIS", "//urlEIS"),
        placing_way=_text(data, "purchaseCodeName", "//purchaseMethodName"),
        etp=_text(data, "electronicPlaceInfo/name", "//electronicPlaceInfo/name"),
        customer=_text(data, "customer/mainInfo/fullName", "//customer/mainInfo/fullName",
                       "//placer/mainInfo/fullName"),
        customer_inn=_text(data, "customer/mainInfo/inn", "//customer/mainInfo/inn"),
        region=region,
        published_at=_dt(_text(data, "publicationDateTime", "//publicationDateTime", "//createDateTime")),
        ends_at=_dt(_text(data, "submissionCloseDateTime", "//submissionCloseDateTime",
                          "//documentationDelivery/deliveryEndDateTime")),
        max_price=sum(sums) if sums else None,
        okpd2=_unique_texts(data, "okpd2", "code"),
        okpd2_names=list(dict.fromkeys(names)),
    )


def parse_notice(xml: bytes | str, region: str = "") -> Notice | None:
    """Извещение 44-ФЗ, 223-ФЗ или ПП РФ 615 из XML. Для других документов (отмены, протоколы...) — None."""
    root = ET.fromstring(xml)
    # Документ бывает обёрнут в <export>/<ns2:...>: само извещение — первый дочерний элемент.
    if _local(root.tag) == "export" and len(root):
        root = root[0]
    doc_type = _local(root.tag)
    law = law_of(doc_type)
    if law is None or "cancel" in doc_type.lower():
        return None
    if law == "223-ФЗ":
        return _parse_223(root, doc_type, region)
    return _parse_44(root, doc_type, law, region)


def eis_url(number: str) -> str:
    """Ссылка на поиск закупки в ЕИС, если в документе её нет."""
    n = re.sub(r"\D", "", number)
    return f"https://zakupki.gov.ru/epz/order/extendedsearch/results.html?searchString={n}"
