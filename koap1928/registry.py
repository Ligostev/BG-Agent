"""Разбор файла ЕИС «Информация о привлечении участника закупки к административной
ответственности по ст. 19.28 КоАП» и поиск в нём по ИНН/ОГРН.

Реестр раньше вела Генпрокуратура на своём сайте, с 30.07.2021 он там не
размещается: сведения публикуются в ЕИС (zakupki.gov.ru) файлом Excel в разделе
«Документы». В файле колонки: ИНН, ОГРН, КПП, наименование, тип участника, суд,
номер дела, дата вынесения постановления, дата вступления в законную силу.
Порядок и точные заголовки колонок не сверены с живым файлом, поэтому колонки
ищутся по словам в заголовке, а строка заголовка — среди первых строк листа.
"""

from __future__ import annotations

import io
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Iterable

# По п. 7.1 ч. 1 ст. 31 44-ФЗ участник не допускается к закупкам, если его
# привлекли к ответственности по ст. 19.28 КоАП в течение двух лет до подачи заявки.
BAN_YEARS = 2

# Ключ поля -> регулярное выражение для заголовка. Порядок важен: «дата вступления»
# проверяется раньше «даты постановления», «наименование суда» раньше наименования юрлица.
COLUMNS: list[tuple[str, re.Pattern]] = [
    ("inn", re.compile(r"^\s*инн\b", re.I)),
    ("ogrn", re.compile(r"^\s*огрн", re.I)),
    ("kpp", re.compile(r"^\s*кпп\b", re.I)),
    ("effective_date", re.compile(r"вступ|законн", re.I)),
    ("decision_date", re.compile(r"дата.*(постанов|решени|вынес)|вынесен", re.I)),
    ("case_number", re.compile(r"номер.*дел|№.*дел|^\s*дело", re.I)),
    ("court", re.compile(r"\bсуд|\bорган(?!из)", re.I)),
    ("participant_type", re.compile(r"тип|вид.*участ", re.I)),
    ("name", re.compile(r"наимен|название|организац", re.I)),
]


class RegistryFormatError(Exception):
    pass


@dataclass
class Violation:
    """Запись реестра: юрлицо привлечено по ст. 19.28 КоАП."""
    inn: str | None
    ogrn: str | None
    name: str | None
    kpp: str | None = None
    participant_type: str | None = None
    court: str | None = None
    case_number: str | None = None
    decision_date: date | None = None     # дата вынесения постановления
    effective_date: date | None = None    # дата вступления постановления в законную силу

    @property
    def start_date(self) -> date | None:
        """С какой даты считать два года запрета: со вступления в силу, иначе с вынесения."""
        return self.effective_date or self.decision_date

    @property
    def ban_until(self) -> date | None:
        d = self.start_date
        if d is None:
            return None
        try:
            return d.replace(year=d.year + BAN_YEARS)
        except ValueError:  # 29 февраля
            return d.replace(year=d.year + BAN_YEARS, day=28)

    def active(self, today: date | None = None) -> bool:
        """Запрет на участие в закупках ещё действует (или дата неизвестна — считаем действующим)."""
        until = self.ban_until
        return until is None or (today or date.today()) < until

    def describe(self) -> str:
        parts = []
        if self.court:
            parts.append(self.court)
        if self.case_number:
            parts.append(f"дело {self.case_number}")
        if self.decision_date:
            parts.append(f"постановление от {self.decision_date:%d.%m.%Y}")
        if self.effective_date:
            parts.append(f"вступило в силу {self.effective_date:%d.%m.%Y}")
        return ", ".join(parts) or "сведения о постановлении не указаны"

    def to_dict(self, today: date | None = None) -> dict:
        d = asdict(self)
        for k in ("decision_date", "effective_date"):
            d[k] = d[k].isoformat() if d[k] else None
        until = self.ban_until
        d["ban_until"] = until.isoformat() if until else None
        d["active"] = self.active(today)
        return d


def _digits(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = re.sub(r"\D", "", str(v))
    return s or None


def _text(v: Any) -> str | None:
    if v is None:
        return None
    s = re.sub(r"\s+", " ", str(v)).strip()
    return s or None


def parse_date(v: Any) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:19] if "T" in s else s.split()[0], fmt).date()
        except (ValueError, IndexError):
            continue
    return None


def _inn_fix(inn: str | None) -> str | None:
    """Excel съедает ведущий ноль у ИНН, записанного числом (0274... -> 274...)."""
    if inn and len(inn) in (9, 11):
        return "0" + inn
    return inn


def map_header(row: Iterable[Any]) -> dict[str, int]:
    found: dict[str, int] = {}
    for i, cell in enumerate(row):
        title = _text(cell)
        if not title:
            continue
        for key, rx in COLUMNS:
            if key not in found and rx.search(title):
                found[key] = i
                break
    return found


def parse_rows(rows: Iterable[Iterable[Any]], header_scan: int = 20) -> list[Violation]:
    """Строки листа (списки значений ячеек) -> записи реестра."""
    rows = iter(rows)
    header: dict[str, int] = {}
    for _ in range(header_scan):
        try:
            row = list(next(rows))
        except StopIteration:
            break
        header = map_header(row)
        if "inn" in header or "ogrn" in header:
            break
    else:
        header = {}
    if "inn" not in header and "ogrn" not in header:
        raise RegistryFormatError("в файле не найдена строка заголовка с колонкой ИНН или ОГРН")

    def cell(row: list, key: str) -> Any:
        i = header.get(key)
        return row[i] if i is not None and i < len(row) else None

    out = []
    for row in rows:
        row = list(row)
        inn, ogrn = _inn_fix(_digits(cell(row, "inn"))), _digits(cell(row, "ogrn"))
        if not inn and not ogrn:
            continue
        out.append(Violation(
            inn=inn, ogrn=ogrn, name=_text(cell(row, "name")), kpp=_digits(cell(row, "kpp")),
            participant_type=_text(cell(row, "participant_type")), court=_text(cell(row, "court")),
            case_number=_text(cell(row, "case_number")),
            decision_date=parse_date(cell(row, "decision_date")),
            effective_date=parse_date(cell(row, "effective_date"))))
    return out


def parse_xlsx(content: bytes) -> list[Violation]:
    if content[:4] == b"\xd0\xcf\x11\xe0":
        raise RegistryFormatError("файл в старом формате .xls; пересохраните его в .xlsx")
    if content[:2] != b"PK":
        raise RegistryFormatError("файл не похож на Excel (.xlsx)")
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    try:
        out: list[Violation] = []
        errors = []
        for ws in wb.worksheets:
            try:
                out.extend(parse_rows(ws.iter_rows(values_only=True)))
            except RegistryFormatError as e:
                errors.append(e)
        if not out and errors:
            raise errors[0]
        return out
    finally:
        wb.close()


class Registry:
    """Реестр в памяти с индексом по ИНН и ОГРН."""

    def __init__(self, records: list[Violation], loaded_at: datetime | None = None,
                 source: str | None = None):
        self.records = records
        self.loaded_at = loaded_at or datetime.now()
        self.source = source
        self._by_id: dict[str, list[Violation]] = {}
        for r in records:
            for ident in {r.inn, r.ogrn} - {None}:
                self._by_id.setdefault(ident, []).append(r)

    def __len__(self) -> int:
        return len(self.records)

    def find(self, *idents: str | None) -> list[Violation]:
        """Точные совпадения по ИНН и/или ОГРН, новые постановления первыми."""
        seen: dict[int, Violation] = {}
        for ident in idents:
            for r in self._by_id.get(_digits(ident) or "", []):
                seen[id(r)] = r
        return sorted(seen.values(), key=lambda r: r.start_date or date.min, reverse=True)
