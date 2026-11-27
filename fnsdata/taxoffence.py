"""Сведения о налоговых правонарушениях и мерах ответственности (набор 7707329152-taxoffence).

ФНС публикует по каждой организации сумму штрафов за налоговые правонарушения
(НК РФ) за прошлый год. Набор — zip с множеством XML, примерно так:

    <Документ ИдДок="..." ДатаДок="..." ДатаСост="...">
      <СведНП НаимОрг="..." ИННЮЛ="..."/>
      <СведНаруш СумШтраф="..."/>
    </Документ>

Структура не сверена с живым файлом, поэтому разбор терпимый: ИНН берётся из
любого атрибута ИННЮЛ внутри документа, штраф — сумма атрибутов, начинающихся на
«СумШтраф». Набор большой, поэтому он один раз раскладывается в SQLite рядом с
архивом, а проверка читает только индекс.
"""

from __future__ import annotations

import sqlite3
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TaxOffence:
    inn: str
    name: str | None
    fine: float
    doc_date: str | None    # дата, на которую составлены сведения


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_doc(el: ET.Element) -> TaxOffence | None:
    inn = name = None
    fine = 0.0
    for node in el.iter():
        for k, v in node.attrib.items():
            k = _local(k)
            if k == "ИННЮЛ" and not inn:
                inn = v.strip()
            elif k == "НаимОрг" and not name:
                name = v.strip()
            elif k.startswith("СумШтраф"):
                try:
                    fine += float(v.replace(",", "."))
                except ValueError:
                    pass
    if not inn:
        return None
    return TaxOffence(inn=inn, name=name, fine=fine,
                      doc_date=el.attrib.get("ДатаСост") or el.attrib.get("ДатаДок"))


def iter_offences(archive: str | Path):
    with zipfile.ZipFile(archive) as z:
        for member in z.namelist():
            if not member.lower().endswith(".xml"):
                continue
            with z.open(member) as f:
                for _, el in ET.iterparse(f, events=("end",)):
                    if _local(el.tag) == "Документ":
                        rec = _parse_doc(el)
                        if rec:
                            yield rec
                        el.clear()


def build_taxoffence_index(archive: str | Path, db_path: str | Path) -> int:
    """Разложить архив в SQLite. Пишется во временный файл и подменяет старый целиком."""
    db_path = Path(db_path)
    tmp = db_path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    try:
        con.execute("CREATE TABLE offence (inn TEXT, name TEXT, fine REAL, doc_date TEXT)")
        n = 0
        batch = []
        for rec in iter_offences(archive):
            batch.append((rec.inn, rec.name, rec.fine, rec.doc_date))
            if len(batch) >= 5000:
                con.executemany("INSERT INTO offence VALUES (?,?,?,?)", batch)
                n += len(batch)
                batch.clear()
        con.executemany("INSERT INTO offence VALUES (?,?,?,?)", batch)
        n += len(batch)
        con.execute("CREATE INDEX offence_inn ON offence (inn)")
        con.execute("CREATE TABLE meta (archive TEXT, mtime REAL)")
        con.execute("INSERT INTO meta VALUES (?, ?)", (Path(archive).name, Path(archive).stat().st_mtime))
        con.commit()
    finally:
        con.close()
    tmp.replace(db_path)
    return n


class TaxOffenceIndex:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    def is_current(self, archive: str | Path) -> bool:
        if not self.db_path.exists():
            return False
        con = sqlite3.connect(self.db_path)
        try:
            row = con.execute("SELECT archive, mtime FROM meta").fetchone()
        except sqlite3.Error:
            return False
        finally:
            con.close()
        return bool(row) and row[0] == Path(archive).name and row[1] == Path(archive).stat().st_mtime

    def count(self) -> int:
        con = sqlite3.connect(self.db_path)
        try:
            return con.execute("SELECT COUNT(*) FROM offence").fetchone()[0]
        finally:
            con.close()

    def find(self, inn: str) -> list[TaxOffence]:
        con = sqlite3.connect(self.db_path)
        try:
            rows = con.execute("SELECT inn, name, fine, doc_date FROM offence WHERE inn = ? "
                               "ORDER BY doc_date DESC", (inn,)).fetchall()
        finally:
            con.close()
        return [TaxOffence(*r) for r in rows]
