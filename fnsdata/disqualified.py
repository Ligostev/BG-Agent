"""Реестр дисквалифицированных лиц ФНС (набор 7707329152-registerdisqualified).

Дисквалификация — административное наказание по КоАП (ст. 3.11): человеку
запрещено руководить юрлицом на срок от 6 месяцев до 3 лет. В наборе на каждое
постановление: ФИО, дата и место рождения, организация и её ИНН (где человек
работал, когда нарушил), должность, статья КоАП, орган, составивший протокол,
судья, срок, даты начала и окончания. Заголовки колонок не сверены с живым файлом,
поэтому ищутся по словам.

Поиск: по ИНН организации (у кого были дисквалифицированные руководители) и по ФИО
руководителей из карточки ФНС. ИНН самого человека в наборе нет, поэтому совпадение
по ФИО может быть однофамильцем — в отчёте это помечено, дату рождения надо сверить.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from koap1928.registry import parse_date

COLUMNS: list[tuple[str, re.Pattern]] = [
    ("birth_date", re.compile(r"дата.*рожд", re.I)),
    ("birth_place", re.compile(r"мест.*рожд", re.I)),
    ("org_inn", re.compile(r"инн", re.I)),
    ("start", re.compile(r"начал|(дата|срок).*\bс\b", re.I)),
    ("end", re.compile(r"оконч|истеч|(дата|срок).*\bпо\b", re.I)),
    ("period", re.compile(r"срок|период", re.I)),
    ("authority", re.compile(r"орган(?!из)|протокол", re.I)),
    ("article", re.compile(r"стать|коап", re.I)),
    ("judge_position", re.compile(r"должн.*суд", re.I)),
    ("judge", re.compile(r"суд", re.I)),
    ("position", re.compile(r"должн", re.I)),
    ("org_name", re.compile(r"организ|наимен|юрид", re.I)),
    ("fio", re.compile(r"фио|ф\.и\.о|фамил", re.I)),
]
# Короткие технические имена колонок (G1..G13) — на случай, если в файле нет русских заголовков.
G_COLUMNS = ["fio", "birth_date", "birth_place", "org_name", "org_inn", "position", "article",
             "authority", "judge", "judge_position", "period", "start", "end"]


@dataclass
class Disqualification:
    fio: str
    birth_date: date | None = None
    birth_place: str | None = None
    org_name: str | None = None
    org_inn: str | None = None
    position: str | None = None
    article: str | None = None
    authority: str | None = None
    judge: str | None = None
    judge_position: str | None = None
    period: str | None = None
    start: date | None = None
    end: date | None = None

    def active(self, today: date | None = None) -> bool:
        return self.end is None or (today or date.today()) <= self.end

    def describe(self) -> str:
        parts = [self.fio]
        if self.birth_date:
            parts.append(f"р. {self.birth_date:%d.%m.%Y}")
        if self.position or self.org_name:
            parts.append(" ".join(filter(None, [self.position, self.org_name])))
        if self.article:
            parts.append(f"ст. {self.article}" if re.match(r"^\d", self.article) else self.article)
        if self.start or self.end:
            span = " — ".join(f"{d:%d.%m.%Y}" for d in (self.start, self.end) if d)
            parts.append(f"срок {span}")
        elif self.period:
            parts.append(f"срок {self.period}")
        return ", ".join(parts)

    def to_dict(self, today: date | None = None) -> dict:
        d = asdict(self)
        for k in ("birth_date", "start", "end"):
            d[k] = d[k].isoformat() if d[k] else None
        d["active"] = self.active(today)
        return d


def norm_fio(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").lower().replace("ё", "е")).strip()


def _text(v: Any) -> str | None:
    s = re.sub(r"\s+", " ", str(v)).strip() if v is not None else ""
    return s or None


def _decode(content: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def map_header(row: Iterable[Any]) -> dict[str, int]:
    cells = [_text(c) or "" for c in row]
    if cells and all(re.fullmatch(r"G\d+", c, re.I) for c in cells if c):
        return {G_COLUMNS[int(c[1:]) - 1]: i for i, c in enumerate(cells)
                if c and 0 < int(c[1:]) <= len(G_COLUMNS)}
    found: dict[str, int] = {}
    for i, title in enumerate(cells):
        for key, rx in COLUMNS:
            if key not in found and rx.search(title):
                found[key] = i
                break
    return found


def parse_disqualified(content: bytes) -> list[Disqualification]:
    """CSV (или zip с CSV) набора -> записи."""
    if content[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            name = next((n for n in z.namelist() if n.lower().endswith(".csv")), None)
            if not name:
                raise ValueError("в архиве нет CSV")
            content = z.read(name)
    text = _decode(content)
    first = text.split("\n", 1)[0]
    delim = max(";,\t", key=first.count)
    rows = csv.reader(io.StringIO(text), delimiter=delim)
    header = map_header(next(rows, []))
    if "fio" not in header:
        raise ValueError("в файле дисквалифицированных лиц не найдена колонка ФИО")

    def cell(row, key):
        i = header.get(key)
        return row[i] if i is not None and i < len(row) else None

    out = []
    for row in rows:
        fio = _text(cell(row, "fio"))
        if not fio:
            continue
        inn = re.sub(r"\D", "", cell(row, "org_inn") or "") or None
        out.append(Disqualification(
            fio=fio, birth_date=parse_date(cell(row, "birth_date")),
            birth_place=_text(cell(row, "birth_place")), org_name=_text(cell(row, "org_name")),
            org_inn=inn, position=_text(cell(row, "position")), article=_text(cell(row, "article")),
            authority=_text(cell(row, "authority")), judge=_text(cell(row, "judge")),
            judge_position=_text(cell(row, "judge_position")),
            period=_text(cell(row, "period")), start=parse_date(cell(row, "start")),
            end=parse_date(cell(row, "end"))))
    return out


class DisqualifiedRegistry:
    def __init__(self, records: list[Disqualification], source: str | None = None):
        self.records = records
        self.source = source
        self.by_inn: dict[str, list[Disqualification]] = {}
        self.by_fio: dict[str, list[Disqualification]] = {}
        for r in records:
            if r.org_inn:
                self.by_inn.setdefault(r.org_inn, []).append(r)
            self.by_fio.setdefault(norm_fio(r.fio), []).append(r)

    @classmethod
    def load(cls, path: str | Path, source: str | None = None) -> "DisqualifiedRegistry":
        return cls(parse_disqualified(Path(path).read_bytes()), source=source)

    def __len__(self) -> int:
        return len(self.records)

    def by_org(self, inn: str) -> list[Disqualification]:
        return list(self.by_inn.get(inn, []))

    def by_name(self, fio: str) -> list[Disqualification]:
        return list(self.by_fio.get(norm_fio(fio), []))
