"""Проверка по реестру ст. 19.28 КоАП: сам контрагент и связанные компании."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .registry import Registry, Violation


@dataclass
class Hit:
    inn: str
    name: str
    role: str | None
    records: list[Violation]


@dataclass
class Koap1928Report:
    subject: Hit
    related: list[Hit] = field(default_factory=list)   # только связанные компании с записями
    related_checked: int = 0
    registry_size: int = 0
    registry_date: str | None = None
    stale_reason: str | None = None
    today: date = field(default_factory=date.today)

    @property
    def active(self) -> list[Violation]:
        return [r for r in self.subject.records if r.active(self.today)]

    @property
    def flagged(self) -> bool:
        """Действующий запрет у самого контрагента — стоп-фактор для гарантии по 44-ФЗ."""
        return bool(self.active)

    @property
    def verdict(self) -> str:
        if self.active:
            until = max((r.ban_until for r in self.active if r.ban_until), default=None)
            return ("В реестре 19.28: участие в закупках запрещено"
                    + (f" до {until:%d.%m.%Y}" if until else ""))
        if self.subject.records:
            return "Был в реестре 19.28, двухлетний запрет истёк"
        return "В реестре 19.28 не найден"

    def summary(self) -> list[tuple[str, str]]:
        rows = [("Итог", self.verdict)]
        for r in self.subject.records:
            mark = "Действует" if r.active(self.today) else "Истёк"
            until = f", запрет до {r.ban_until:%d.%m.%Y}" if r.ban_until else ""
            rows.append((f"Постановление ({mark.lower()})", r.describe() + until))
        if self.related_checked:
            if self.related:
                for h in self.related:
                    active = any(r.active(self.today) for r in h.records)
                    rows.append((f"Связанная компания: {h.role or 'связь'}",
                                 f"{h.name} (ИНН {h.inn}): {h.records[0].describe()}"
                                 + ("" if active else ", запрет истёк")))
            else:
                rows.append(("Связанные компании", f"Проверено {self.related_checked}, в реестре нет"))
        info = f"{self.registry_size} записей"
        if self.registry_date:
            info += f", загружен {self.registry_date}"
        if self.stale_reason:
            info += f"; ЕИС не ответила, использована прошлая копия ({self.stale_reason})"
        rows.append(("Реестр ЕИС", info))
        return rows

    def to_dict(self) -> dict:
        def hit(h: Hit) -> dict:
            return {"inn": h.inn, "name": h.name, "role": h.role,
                    "records": [r.to_dict(self.today) for r in h.records]}
        return {"flagged": self.flagged, "verdict": self.verdict, "subject": hit(self.subject),
                "related": [hit(h) for h in self.related], "related_checked": self.related_checked,
                "registry_size": self.registry_size, "registry_date": self.registry_date,
                "stale_reason": self.stale_reason}


def check_koap1928(registry: Registry, inn: str, name: str | None = None, ogrn: str | None = None,
                   related: list[tuple[str, str, str | None]] = (), stale_reason: str | None = None,
                   today: date | None = None) -> Koap1928Report:
    """related: (ИНН, название, роль) связанных компаний, например из карточки ФНС."""
    records = registry.find(inn, ogrn)
    subject = Hit(inn=inn, name=name or (records[0].name if records else None) or inn,
                  role=None, records=records)
    own = {inn, ogrn} | {r.inn for r in records} | {r.ogrn for r in records}
    hits, checked = [], 0
    for r_inn, r_name, role in related:
        if not r_inn or r_inn in own:
            continue
        checked += 1
        found = registry.find(r_inn)
        if found:
            hits.append(Hit(inn=r_inn, name=r_name or found[0].name or r_inn, role=role, records=found))
    return Koap1928Report(subject=subject, related=hits, related_checked=checked,
                          registry_size=len(registry),
                          registry_date=f"{registry.loaded_at:%d.%m.%Y %H:%M}",
                          stale_reason=stale_reason, today=today or date.today())
