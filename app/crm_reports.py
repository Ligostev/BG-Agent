"""Аналитика CRM: воронка, выдачи и комиссия, источники клиентов, менеджеры и банки."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .crm_tasks import MSK, aware
from .models import Deal, DealStage


@dataclass
class Row:
    name: str
    deals: int = 0
    issued: int = 0
    lost: int = 0
    amount: float = 0.0
    commission: float = 0.0

    @property
    def conversion(self) -> float | None:
        closed = self.issued + self.lost
        return self.issued / closed if closed else None


@dataclass
class BankRow:
    name: str
    sent: int = 0
    approved: int = 0
    rejected: int = 0
    issued: int = 0
    fees: list = field(default_factory=list)

    @property
    def approval(self) -> float | None:
        answered = self.approved + self.rejected + self.issued
        return (self.approved + self.issued) / answered if answered else None

    @property
    def avg_fee(self) -> float | None:
        return sum(self.fees) / len(self.fees) if self.fees else None


def local_day(dt: datetime | None) -> date | None:
    return aware(dt).astimezone(MSK).date() if dt else None


def month_start(d: date, back: int = 0) -> date:
    y, m = d.year, d.month - back
    while m <= 0:
        y, m = y - 1, m + 12
    return date(y, m, 1)


def build(deals: list[Deal], start: date, end: date, today: date) -> dict:
    """deals — все сделки, видимые пользователю, с загруженными client, client.manager и banks."""
    created = [d for d in deals if start <= local_day(d.created_at) <= end]
    closed = [d for d in deals if d.closed_at and start <= local_day(d.closed_at) <= end]
    issued = [d for d in closed if d.stage == DealStage.ISSUED]
    lost = [d for d in closed if d.stage == DealStage.LOST]

    funnel = []
    reached = {s: 0 for s in DealStage.TITLES}
    order = [DealStage.NEW, DealStage.DOCS, DealStage.BANKS, DealStage.APPROVED, DealStage.ISSUED]
    for d in created:
        if d.stage == DealStage.LOST:
            reached[DealStage.LOST] += 1
            # До какого этапа дошла отказная сделка, не знаем: считаем её только на входе.
            reached[DealStage.NEW] += 1
            continue
        for s in order[:order.index(d.stage) + 1]:
            reached[s] += 1
    total = len(created) or 1
    for s in order:
        funnel.append((DealStage.TITLES[s], reached[s], reached[s] / total if created else 0))

    def grouped(key) -> list[Row]:
        rows: dict[str, Row] = {}
        for d in created:
            rows.setdefault(key(d), Row(key(d))).deals += 1
        for d in closed:
            r = rows.setdefault(key(d), Row(key(d)))
            if d.stage == DealStage.ISSUED:
                r.issued += 1
                r.amount += d.amount or 0
                r.commission += d.commission or 0
            else:
                r.lost += 1
        return sorted(rows.values(), key=lambda r: (-r.commission, -r.issued, -r.deals, r.name))

    banks: dict[str, BankRow] = {}
    for d in deals:
        for b in d.banks:
            if not (start <= local_day(b.updated_at) <= end) or b.status == "preparing":
                continue
            row = banks.setdefault(b.bank, BankRow(b.bank))
            row.sent += 1
            if b.status in ("approved", "rejected", "issued"):
                setattr(row, b.status, getattr(row, b.status) + 1)
            if b.fee:
                row.fees.append(b.fee)

    months = []
    for back in range(5, -1, -1):
        m0 = month_start(today, back)
        m1 = month_start(today, back - 1) - timedelta(days=1) if back else today
        done = [d for d in deals if d.stage == DealStage.ISSUED and d.closed_at
                and m0 <= local_day(d.closed_at) <= m1]
        months.append((m0, len(done), sum(d.commission or 0 for d in done)))
    top = max([m[2] for m in months] + [1])

    return {
        "created": len(created), "issued": len(issued), "lost": len(lost),
        "issued_amount": sum(d.amount or 0 for d in issued),
        "commission": sum(d.commission or 0 for d in issued),
        "conversion": len(issued) / (len(issued) + len(lost)) if (issued or lost) else None,
        "in_work": sum(1 for d in deals if d.stage in DealStage.OPEN),
        "in_work_amount": sum(d.amount or 0 for d in deals if d.stage in DealStage.OPEN),
        "funnel": funnel,
        "sources": grouped(lambda d: d.client.source or "не указан"),
        "managers": grouped(lambda d: d.client.manager.email if d.client.manager else "без ответственного"),
        "products": grouped(lambda d: d.product),
        "banks": sorted(banks.values(), key=lambda r: (-r.issued, -r.approved, -r.sent, r.name)),
        "months": [(m, n, c, c / top) for m, n, c in months],
    }
