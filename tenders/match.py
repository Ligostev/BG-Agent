"""Профиль клиента и отбор подходящих закупок.

Правила простые и объяснимые, без нейросети: клиент видит, почему закупка попала
в подборку, а брокер — какая гарантия и на какую сумму понадобится.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .imports import Contract
from .notice import Notice
from .regions import REGIONS

OKPD_RE = re.compile(r"(?<![\d.])\d{2}(?:\.\d{1,2}(?:\.\d{2}(?:\.\d{3})?)?)?(?![\d])")
SMR_OKPD2 = ("41.", "42.", "43.")


@dataclass
class Profile:
    okpd2: list[str] = field(default_factory=list)     # префиксы кодов: "43", "43.21", "41.20.40.000"
    regions: list[str] = field(default_factory=list)   # коды субъектов: "77", "50"
    price_min: float | None = None
    price_max: float | None = None
    min_days_left: int = 2                             # успеть собрать заявку и выпустить БГ
    keywords: list[str] = field(default_factory=list)  # слова в предмете закупки (если нет ОКПД2)


@dataclass
class Match:
    notice: Notice
    score: int
    reasons: list[str]
    products: list[tuple[str, float | None]]           # [(вид БГ, сумма)]


def parse_codes(raw: str) -> list[str]:
    """ОКПД2 из произвольного текста: «43.21, 43.22.11.000; 41» -> ['43.21', '43.22.11.000', '41']."""
    out = []
    for code in OKPD_RE.findall(raw or ""):
        if code not in out:
            out.append(code)
    return out


def parse_regions(raw: str) -> list[str]:
    out = []
    for code in re.findall(r"(?<!\d)\d{1,2}(?!\d)", raw or ""):
        code = code.zfill(2)
        if code in REGIONS and code not in out:
            out.append(code)
    return out


def _okpd_hit(codes: list[str], prefixes: list[str]) -> str | None:
    """Самый длинный префикс профиля, с которого начинается какой-то код закупки."""
    best = None
    for p in prefixes:
        for c in codes:
            if c == p or c.startswith(p + ".") or (len(p) > 2 and c.startswith(p)):
                if best is None or len(p) > len(best):
                    best = p
    return best


def guarantee_products(n: Notice) -> list[tuple[str, float | None]]:
    out: list[tuple[str, float | None]] = []
    if n.application_guarantee:
        out.append(("БГ на участие (обеспечение заявки)", n.application_guarantee))
    if n.contract_guarantee:
        out.append(("БГ на исполнение контракта", n.contract_guarantee))
    if n.warranty_guarantee:
        out.append(("БГ на гарантийные обязательства", n.warranty_guarantee))
    if not out and n.max_price:
        out.append(("БГ на исполнение контракта (размер уточнить в документации)", None))
    if any(c.startswith(SMR_OKPD2) for c in n.okpd2):
        out.append(("страхование СМР (если требует контракт)", None))
    return out


def match_notice(n: Notice, p: Profile, now: datetime | None = None) -> Match | None:
    """Закупка подходит клиенту -> Match с причинами, иначе None."""
    now = now or datetime.now(timezone.utc)
    reasons: list[str] = []
    score = 0

    if p.regions:
        if n.region not in p.regions:
            return None
        reasons.append(f"регион: {REGIONS.get(n.region, n.region)}")
        score += 20

    if p.okpd2:
        hit = _okpd_hit(n.okpd2, p.okpd2)
        if hit is None:
            return None
        reasons.append(f"ОКПД2 {hit}")
        score += 30 + min(len(hit), 12) * 2
    elif p.keywords:
        text = n.subject.lower()
        words = [w for w in p.keywords if w.lower() in text]
        if not words:
            return None
        reasons.append("в предмете: " + ", ".join(words))
        score += 25
    else:
        return None   # без кодов и слов подбирать не из чего

    if n.max_price is not None:
        if p.price_max and n.max_price > p.price_max:
            return None
        if p.price_min and n.max_price < p.price_min:
            return None
        if p.price_min or p.price_max:
            reasons.append("НМЦК в вашем диапазоне")
            score += 15

    if n.ends_at is not None:
        ends = n.ends_at if n.ends_at.tzinfo else n.ends_at.replace(tzinfo=timezone.utc)
        days = (ends - now).total_seconds() / 86400
        if days < p.min_days_left:
            return None
        reasons.append(f"подача до {ends.strftime('%d.%m.%Y')}")
        score += 15 if days >= 7 else 8

    return Match(notice=n, score=min(score, 100), reasons=reasons, products=guarantee_products(n))


def profile_from_contracts(contracts: list[Contract], base: Profile | None = None) -> Profile:
    """Профиль по прошлым контрактам: частые ОКПД2 (до класса XX.YY), регионы и диапазон цен."""
    p = Profile(**vars(base)) if base else Profile()
    codes = Counter()
    for c in contracts:
        for code in parse_codes(c.okpd2):
            codes[".".join(code.split(".")[:2])] += 1
    top = [c for c, _ in codes.most_common(10)]
    p.okpd2 = list(dict.fromkeys([*p.okpd2, *top]))
    regions = []
    for c in contracts:
        for code, name in REGIONS.items():
            if c.region and (c.region.strip().zfill(2) == code or name.lower() in c.region.lower()):
                regions.append(code)
    p.regions = list(dict.fromkeys([*p.regions, *regions]))
    prices = sorted(c.price for c in contracts if c.price)
    if prices:
        # От половины самого маленького до двух самых крупных контрактов клиента.
        p.price_min = p.price_min or round(prices[0] / 2, -3)
        p.price_max = p.price_max or round(prices[-1] * 2, -3)
    return p
