"""Сводная проверка банкротства: сам контрагент, сообщения кредиторов и связанные лица."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .client import Bankruptcy, FedresursClient, Publication
from .parties import Party

log = logging.getLogger(__name__)


@dataclass
class PartyResult:
    party: Party
    records: list[Bankruptcy] = field(default_factory=list)
    error: str | None = None

    @property
    def bankrupt(self) -> bool:
        return bool(self.records)


@dataclass
class BankruptcyReport:
    subject: PartyResult
    intentions: list[Publication] = field(default_factory=list)
    intentions_error: str | None = None
    related: list[PartyResult] = field(default_factory=list)

    @property
    def flagged(self) -> bool:
        return (self.subject.bankrupt or bool(self.intentions)
                or any(r.bankrupt for r in self.related))

    def summary(self) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        rows.append(("Итог", "Есть признаки банкротства" if self.flagged
                     else "Признаков банкротства не найдено"))
        who = "Компания" if self.subject.party.kind == "company" else "Физлицо / ИП"
        rows.append((who, _describe(self.subject)))
        if self.subject.party.kind == "company":
            if self.intentions_error:
                rows.append(("Намерения кредиторов", f"не удалось проверить: {self.intentions_error}"))
            elif self.intentions:
                last = max((p.date or "" for p in self.intentions), default="")
                text = f"{len(self.intentions)} сообщ."
                rows.append(("Намерения кредиторов",
                             f"{text}, последнее {last[:10]}" if last else text))
            else:
                rows.append(("Намерения кредиторов", "Нет"))
        if not self.related:
            rows.append(("Связанные лица", "В карточке ФНС не найдено лиц с ИНН"))
            return rows
        bad = [r for r in self.related if r.bankrupt]
        failed = [r for r in self.related if r.error]
        checked = len(self.related) - len(failed)
        rows.append(("Связанные лица", f"Проверено {checked}, банкротства: {len(bad)}"))
        for r in bad:
            rows.append((f"{r.party.role}: {r.party.name} (ИНН {r.party.inn})", _describe(r)))
        if failed:
            rows.append(("Не удалось проверить",
                         "; ".join(f"{r.party.name} (ИНН {r.party.inn})" for r in failed)))
        return rows

    def to_dict(self) -> dict:
        def pr(r: PartyResult) -> dict:
            return {"inn": r.party.inn, "name": r.party.name, "kind": r.party.kind,
                    "roles": r.party.roles, "error": r.error,
                    "bankruptcies": [{"name": b.name, "inn": b.inn, "case_number": b.case_number,
                                      "status": b.status, "active": b.active, "url": b.url}
                                     for b in r.records]}
        return {"flagged": self.flagged, "subject": pr(self.subject),
                "creditor_intentions": [vars(p) for p in self.intentions],
                "creditor_intentions_error": self.intentions_error,
                "related": [pr(r) for r in self.related]}


def _describe(r: PartyResult) -> str:
    if r.error:
        return f"не удалось проверить: {r.error}"
    if not r.records:
        return "Сведений о банкротстве нет"
    return "; ".join(("Банкрот: " if b.active else "Было банкротство: ") + b.describe()
                     for b in r.records)


async def _check_party(client: FedresursClient, party: Party) -> PartyResult:
    try:
        return PartyResult(party, await client.bankruptcies(party.inn))
    except Exception as e:  # одно лицо не должно ронять всю проверку
        log.warning("банкротство %s: %s", party.inn, e)
        return PartyResult(party, error=f"{type(e).__name__}: {e}")


async def check_bankruptcy(client: FedresursClient, subject: Party,
                           related: list[Party] | None = None,
                           intentions: bool = True) -> BankruptcyReport:
    """Ошибка по самому контрагенту пробрасывается, по связанным лицам сохраняется в отчёте."""
    report = BankruptcyReport(PartyResult(subject, await client.bankruptcies(subject.inn)))
    if intentions and subject.kind == "company" and len(subject.inn) == 10:
        try:
            report.intentions = await client.creditor_intentions(subject.inn)
        except Exception as e:
            report.intentions_error = f"{type(e).__name__}: {e}"
    for party in related or []:  # по очереди: у клиента общий лимит запросов
        report.related.append(await _check_party(client, party))
    return report
