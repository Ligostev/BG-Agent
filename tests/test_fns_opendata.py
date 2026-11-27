"""Открытые данные ФНС (дисквалифицированные, налоговые правонарушения) на имитации сайта."""

import asyncio
import io
import zipfile
from datetime import date

import httpx

from app.sources import SourceContext, SourceData
from app.sources.fns_opendata import DISQUALIFIED, TAXOFFENCE, DisqualifiedSource, TaxOffenceSource
from fnsdata import (DisqualifiedRegistry, OpenDataset, TaxOffenceIndex, build_taxoffence_index,
                     find_data_url, parse_disqualified)

SUBJ = "7707083893"
DIR = "500100732259"

DISQ_CSV = (
    "ФИО;Дата рождения;Место рождения;Наименование организации;ИНН организации;Должность;"
    "Статья КоАП РФ;Наименование органа, составившего протокол;ФИО судьи;Должность судьи;"
    "Срок дисквалификации;Дата начала;Дата окончания\n"
    "Иванов Иван Иванович;01.01.1970;г. Москва;ООО ВАСИЛЕК;7702070139;Генеральный директор;"
    "14.25 ч.5;МИФНС № 46;Петрова А.А.;Мировой судья;1 год;01.03.2026;28.02.2099\n"
    "Сидоров Сидор;02.02.1980;г. Тверь;ООО РОМАШКА;7707083893;Директор;15.6;ИФНС;;;6 мес.;"
    "01.01.2020;30.06.2020\n"
)


def meta(code: str, ext: str) -> str:
    return (f"property,value\nstandardversion,http://opendata.gosmonitor.ru/standard/3.0\n"
            f"data-20250101-structure-20220321,https://file.nalog.ru/opendata/{code}/data-20250101-structure-20220321.{ext}\n"
            f"data-20261101-structure-20220321,https://file.nalog.ru/opendata/{code}/data-20261101-structure-20220321.{ext}\n")


def taxoffence_zip() -> bytes:
    xml = ('<?xml version="1.0" encoding="utf-8"?><Файл><Документ ИдДок="1" ДатаДок="2026-06-01" '
           f'ДатаСост="2026-05-31"><СведНП НаимОрг="ООО РОМАШКА" ИННЮЛ="{SUBJ}"/>'
           '<СведНаруш СумШтраф="12500.50"/></Документ>'
           '<Документ ИдДок="2"><СведНП НаимОрг="ООО ДРУГОЕ" ИННЮЛ="7736050003"/>'
           '<СведНаруш СумШтраф="1000"/></Документ></Файл>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("VO_OTKRDAN_9965_9965_20260601_1.xml", xml.encode("utf-8"))
    return buf.getvalue()


class FakeNalog:
    def __init__(self, fail=False):
        self.fail = fail
        self.downloads = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.fail:
            return httpx.Response(503)
        path = request.url.path
        if path == f"/opendata/{DISQUALIFIED}/meta.csv":
            return httpx.Response(200, text=meta(DISQUALIFIED, "csv"))
        if path == f"/opendata/{TAXOFFENCE}/meta.csv":
            return httpx.Response(200, text=meta(TAXOFFENCE, "zip"))
        if path.endswith("data-20261101-structure-20220321.csv"):
            self.downloads += 1
            return httpx.Response(200, content=DISQ_CSV.encode("cp1251"))
        if path.endswith("data-20261101-structure-20220321.zip"):
            self.downloads += 1
            return httpx.Response(200, content=taxoffence_zip())
        return httpx.Response(404)


def dataset(code, tmp_path, site) -> OpenDataset:
    return OpenDataset(code, tmp_path, http=httpx.AsyncClient(transport=httpx.MockTransport(site)))


FNS = SourceData(found=True, identity={"inn": SUBJ, "title": "ООО РОМАШКА"},
                 data={"hit": {"inn": SUBJ}, "card": {"masruk": [
                     {"name": "Иванов Иван Иванович", "inn": DIR, "position": "Генеральный директор"}]}})


def test_find_data_url_takes_newest():
    assert find_data_url(meta(DISQUALIFIED, "csv")).endswith("data-20261101-structure-20220321.csv")
    assert find_data_url("нет ссылок") is None


def test_parse_disqualified_cp1251_and_fields():
    recs = parse_disqualified(DISQ_CSV.encode("cp1251"))
    assert len(recs) == 2
    r = recs[0]
    assert (r.fio, r.org_inn, r.article, r.authority) == ("Иванов Иван Иванович", "7702070139",
                                                          "14.25 ч.5", "МИФНС № 46")
    assert (r.birth_date, r.start, r.end) == (date(1970, 1, 1), date(2026, 3, 1), date(2099, 2, 28))
    assert r.judge == "Петрова А.А." and r.judge_position == "Мировой судья" and r.period == "1 год"
    assert r.active() and not recs[1].active()
    reg = DisqualifiedRegistry(recs)
    assert reg.by_name("иванов  иван иванович") and reg.by_org(SUBJ)[0].fio == "Сидоров Сидор"


def test_disqualified_source_flags_current_director(tmp_path):
    site = FakeNalog()

    async def run():
        src = DisqualifiedSource(dataset(DISQUALIFIED, tmp_path, site))
        try:
            out = await src.fetch(SourceContext(query=SUBJ, ask_captcha=None, previous={"fns": FNS}))
            await src.fetch(SourceContext(query=SUBJ, ask_captcha=None, previous={"fns": FNS}))
            return out
        finally:
            await src.aclose()

    out = asyncio.run(run())
    assert out.data["flagged"]
    assert out.data["verdict"].startswith("Руководитель (по ФИО) в реестре")
    assert len(out.data["by_org"]) == 1 and not out.data["by_org"][0]["active"]
    labels = [label for label, _ in out.summary]
    assert any("совпадение по ФИО" in label for label in labels)
    assert site.downloads == 1  # второй раз из локальной копии


def test_dataset_falls_back_to_old_copy(tmp_path):
    async def run(site):
        ds = dataset(DISQUALIFIED, tmp_path, site)
        try:
            ds.ttl = 0
            return await ds.path(), ds.stale_reason
        finally:
            await ds.aclose()

    first, _ = asyncio.run(run(FakeNalog()))
    again, reason = asyncio.run(run(FakeNalog(fail=True)))
    assert first == again and "503" in reason


def test_taxoffence_index_and_source(tmp_path):
    archive = tmp_path / "t.zip"
    archive.write_bytes(taxoffence_zip())
    db = tmp_path / "t.db"
    assert build_taxoffence_index(archive, db) == 2
    idx = TaxOffenceIndex(db)
    assert idx.is_current(archive) and idx.find(SUBJ)[0].fine == 12500.5

    async def run(query, previous):
        src = TaxOffenceSource(dataset(TAXOFFENCE, tmp_path / "cache", FakeNalog()))
        try:
            return await src.fetch(SourceContext(query=query, ask_captcha=None, previous=previous))
        finally:
            await src.aclose()

    out = asyncio.run(run(SUBJ, {"fns": FNS}))
    assert out.data["flagged"] and out.data["total_fine"] == 12500.5
    assert out.summary[0] == ("Итог", "Штрафы за налоговые правонарушения: 12 500 ₽")
    clean = asyncio.run(run("7707049388", {}))
    assert not clean.data["flagged"]
    assert not asyncio.run(run(DIR, {})).found  # физлица в наборе нет
