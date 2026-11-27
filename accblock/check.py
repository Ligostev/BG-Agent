"""Сводный отчёт о блокировках счетов и ограничениях на открытие счетов."""

from __future__ import annotations

from dataclasses import dataclass, field

from .fns import BlockDecision
from .zsk import ZSK_PAGE

FNS_PAGE = "https://service.nalog.ru/bi.do"


@dataclass
class FnsResult:
    checked: bool
    decisions: list[BlockDecision] = field(default_factory=list)
    error: str | None = None       # почему не проверили
    bik: str | None = None


@dataclass
class ZskResult:
    checked: bool
    high_risk: bool | None = None  # None — не поняли ответ или не проверяли
    text: str | None = None
    error: str | None = None


@dataclass
class AccountBlockReport:
    inn: str | None
    fns: FnsResult
    zsk: ZskResult

    @property
    def flagged(self) -> bool:
        return bool(self.fns.decisions) or self.zsk.high_risk is True

    @property
    def unavailable(self) -> bool:
        """Главный источник (ФНС) не ответил: «чисто» утверждать нельзя."""
        return not self.flagged and not self.fns.checked

    @property
    def verdict(self) -> str:
        parts = []
        if self.fns.decisions:
            parts.append(f"Счета заблокированы: действующих решений ФНС — {len(self.fns.decisions)}, "
                         "банк не откроет новый счёт")
        if self.zsk.high_risk:
            parts.append("ЦБ: высокий уровень риска (ЗСК)")
        if parts:
            return "; ".join(parts)
        if not self.inn:
            return "Нет ИНН: блокировки счетов не проверялись"
        if not self.fns.checked:
            return "Не удалось проверить блокировки счетов: сервис ФНС не ответил"
        return "Действующих решений о приостановлении операций по счетам нет"

    def summary(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        if not self.inn:
            return [("Статус", "Нет ИНН: проверка счетов возможна только по ИНН")]
        if self.fns.checked:
            if self.fns.decisions:
                out.append(("Решения ФНС о приостановлении",
                            f"{len(self.fns.decisions)} действующих"))
                for i, d in enumerate(self.fns.decisions[:20], 1):
                    out.append((f"Решение {i}", d.label()))
                out.append(("Что это значит", "Операции по счетам приостановлены, новые счета "
                                              "банк открыть не вправе (п. 12 ст. 76 НК РФ)"))
            else:
                out.append(("Решения ФНС о приостановлении", "действующих нет"))
        else:
            out.append(("Решения ФНС о приостановлении",
                        f"не проверено: {self.fns.error or 'сервис не ответил'}. "
                        f"Проверить вручную: {FNS_PAGE}"))
        if self.zsk.checked and self.zsk.high_risk is not None:
            out.append(("ЦБ «Знай своего клиента»",
                        "высокий уровень риска (красная зона): банки ограничивают операции "
                        "и открытие счетов" if self.zsk.high_risk
                        else "не отнесён к группе высокого риска"))
        elif self.zsk.checked:
            out.append(("ЦБ «Знай своего клиента»",
                        f"ответ не распознан: {self.zsk.text or '—'}. Проверить вручную: {ZSK_PAGE}"))
        else:
            out.append(("ЦБ «Знай своего клиента»",
                        f"не проверено автоматически ({self.zsk.error or 'нет ответа'}). "
                        f"Проверить вручную: {ZSK_PAGE}"))
        return out

    def to_dict(self) -> dict:
        return {
            "inn": self.inn,
            "verdict": self.verdict,
            "flagged": self.flagged,
            "unavailable": self.unavailable,
            "fns": {"checked": self.fns.checked, "error": self.fns.error, "bik": self.fns.bik,
                    "decisions": [d.to_dict() for d in self.fns.decisions]},
            "zsk": {"checked": self.zsk.checked, "high_risk": self.zsk.high_risk,
                    "text": self.zsk.text, "error": self.zsk.error, "page": ZSK_PAGE},
        }
