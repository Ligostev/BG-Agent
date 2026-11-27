"""Проверка по реестру ст. 19.28 КоАП на имитации ЕИС (живой сайт из облака недоступен)."""

import asyncio
import io
from datetime import date, datetime

import httpx
import pytest
from openpyxl import Workbook

from app.sources import SourceContext, SourceData
from app.sources.koap1928 import Koap1928Source
from koap1928 import (Koap1928Client, Registry, RegistryFormatError, RegistryUnavailable,
                      check_koap1928, find_file_url, parse_rows, parse_xlsx)

SUBJ = "7707083893"
SUBJ_OGRN = "1027700132195"
OLD = "7736050003"        # было постановление больше двух лет назад
OTHER = "7702070139"      # другая компания руководителя, в реестре
TODAY = date(2026, 11, 27)

HEADER = ["ИНН", "ОГРН", "КПП", "Наименование юридического лица", "Тип участника",
          "Наименование суда", "Номер дела", "Дата вынесения постановления",
          "Дата вступления постановления в законную силу"]
ROWS = [
    [int(SUBJ), int(SUBJ_OGRN), "770701001", "ООО «РОМАШКА»", "Юридическое лицо РФ",
     "Арбитражный суд г. Москвы", "А40-111/2026", datetime(2026, 3, 10), datetime(2026, 4, 15)],
    [OLD, None, None, "ООО СТАРОЕ", "ЮЛ", "Мировой судья судебного участка № 5", "5-12/2023",
     "01.02.2023", "20.03.2023"],
    [OTHER, None, None, "ООО ВАСИЛЕК", "ЮЛ", "Мировой судья", "5-1/2026", "01.06.2026", None],
    [None, None, None, None, None, None, None, None, None],
]


def make_xlsx(header=HEADER, rows=ROWS, title_rows=2) -> bytes:
    wb = Workbook()
    ws = wb.active
    for i in range(title_rows):  # над таблицей обычно заголовок документа
        ws.append([f"Информация о привлечении участника закупки к ответственности по ст. 19.28 КоАП ({i})"])
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


PAGE = """<html><body>
<div class="doc"><a href="/epz/main/public/download/downloadDocument.html?id=100">Регламент ЕИС</a></div>
<div class="doc"><span>Информация о привлечении участника закупки к административной
ответственности по ст. 19.28 КоАП</span>
<a href="/epz/main/public/download/downloadDocument.html?id=55">скачать</a></div>
</body></html>"""


class FakeEis:
    def __init__(self, content: bytes | None = None, page_status: int = 200):
        self.content = content if content is not None else make_xlsx()
        self.page_status = page_status
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        if request.url.path == "/epz/main/public/document/view.html":
            assert request.url.params["sectionId"] == "2369"
            return httpx.Response(self.page_status, text=PAGE)
        if request.url.path == "/epz/main/public/download/downloadDocument.html":
            assert request.url.params["id"] == "55"
            return httpx.Response(200, content=self.content)
        return httpx.Response(404)


def make_client(site, **kw) -> Koap1928Client:
    return Koap1928Client(http=httpx.AsyncClient(transport=httpx.MockTransport(site)),
                          file_path="", file_url="", cache_path=kw.pop("cache_path", ""), **kw)


def test_parse_xlsx_finds_header_and_fields():
    recs = parse_xlsx(make_xlsx())
    assert len(recs) == 3
    r = recs[0]
    assert (r.inn, r.ogrn, r.name, r.case_number) == (SUBJ, SUBJ_OGRN, "ООО «РОМАШКА»", "А40-111/2026")
    assert r.court == "Арбитражный суд г. Москвы" and r.participant_type == "Юридическое лицо РФ"
    assert (r.decision_date, r.effective_date) == (date(2026, 3, 10), date(2026, 4, 15))
    assert recs[1].effective_date == date(2023, 3, 20)


def test_parse_rows_restores_leading_zero_and_rejects_bad_files():
    recs = parse_rows([["ИНН", "Наименование"], [274062111, "ООО БАШ"]])
    assert recs[0].inn == "0274062111"
    with pytest.raises(RegistryFormatError):
        parse_rows([["Название", "Суд"], ["x", "y"]])
    with pytest.raises(RegistryFormatError):
        parse_xlsx(b"\xd0\xcf\x11\xe0old xls")


def test_ban_period_two_years_from_effective_date():
    reg = Registry(parse_xlsx(make_xlsx()))
    rep = check_koap1928(reg, inn=SUBJ, today=TODAY)
    assert rep.flagged
    assert rep.verdict == "В реестре 19.28: участие в закупках запрещено до 15.04.2028"
    assert rep.to_dict()["subject"]["records"][0]["ban_until"] == "2028-04-15"

    old = check_koap1928(reg, inn=OLD, today=TODAY)
    assert not old.flagged and old.verdict.startswith("Был в реестре")

    # без даты вступления в силу срок считается от постановления
    other = check_koap1928(reg, inn=OTHER, today=TODAY)
    assert other.subject.records[0].ban_until == date(2028, 6, 1)

    clean = check_koap1928(reg, inn="7707049388", today=TODAY)
    assert not clean.flagged and clean.verdict == "В реестре 19.28 не найден"
    assert ("Реестр ЕИС", clean.summary()[-1][1]) == clean.summary()[-1]


def test_find_by_ogrn_and_related_companies():
    reg = Registry(parse_xlsx(make_xlsx()))
    rep = check_koap1928(reg, inn="", ogrn=SUBJ_OGRN, today=TODAY,
                         related=[(OTHER, "ООО ВАСИЛЕК", "Компания руководителя"),
                                  ("7707049388", "ООО ЧИСТОЕ", "Учредитель"), (SUBJ, "сам", None)])
    assert rep.flagged and rep.related_checked == 2
    assert [h.inn for h in rep.related] == [OTHER]
    labels = [label for label, _ in rep.summary()]
    assert "Связанная компания: Компания руководителя" in labels


def test_find_file_url_prefers_link_near_1928():
    assert find_file_url(PAGE) == "https://zakupki.gov.ru/epz/main/public/download/downloadDocument.html?id=55"
    assert find_file_url("<a href='/x'>нет</a>") is None


def test_client_downloads_and_caches(tmp_path):
    site = FakeEis()
    cache = tmp_path / "koap.xlsx"

    async def run():
        async with make_client(site, cache_path=str(cache)) as c:
            reg1 = await c.registry()
            reg2 = await c.registry()
            return reg1, reg2

    reg1, reg2 = asyncio.run(run())
    assert reg1 is reg2 and len(reg1) == 3
    assert len(site.calls) == 2  # страница + файл, второй раз из памяти
    assert cache.read_bytes() == site.content


def test_client_falls_back_to_disk_copy(tmp_path):
    cache = tmp_path / "koap.xlsx"
    cache.write_bytes(make_xlsx())

    async def run(site, cache_path):
        async with make_client(site, cache_path=cache_path) as c:
            reg = await c.registry()
            return reg, c.stale_reason

    reg, reason = asyncio.run(run(FakeEis(page_status=403), str(cache)))
    assert len(reg) == 3 and "403" in reason
    with pytest.raises(RegistryUnavailable):
        asyncio.run(run(FakeEis(page_status=403), ""))


def test_client_local_file(tmp_path):
    path = tmp_path / "manual.xlsx"
    path.write_bytes(make_xlsx())

    async def run():
        c = Koap1928Client(http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: 1 / 0)),
                           file_path=str(path))
        try:
            return await c.registry()
        finally:
            await c.aclose()

    assert len(asyncio.run(run())) == 3


def test_source_uses_fns_identity_and_related():
    fns = SourceData(found=True, identity={"inn": SUBJ, "ogrn": SUBJ_OGRN, "title": "ООО РОМАШКА"},
                     data={"hit": {"inn": SUBJ},
                           "card": {"masruk": [{"name": "Иванов Иван", "inn": "500100732259",
                                                "companies": [{"namec": "ООО ВАСИЛЕК", "inn": OTHER}]}]}})

    async def run():
        src = Koap1928Source(client=make_client(FakeEis()))
        try:
            return await src.fetch(SourceContext(query=SUBJ, ask_captcha=None, previous={"fns": fns}))
        finally:
            await src.aclose()

    out = asyncio.run(run())
    assert out.found and out.data["flagged"] and out.identity == {}
    assert out.data["subject"]["name"] == "ООО РОМАШКА"
    # физлицо-руководитель не проверяется, его компания — да
    assert out.data["related_checked"] == 1 and out.data["related"][0]["inn"] == OTHER


def test_source_without_fns_takes_identity_from_registry():
    async def run():
        src = Koap1928Source(client=make_client(FakeEis()))
        try:
            return await src.fetch(SourceContext(query=SUBJ_OGRN, ask_captcha=None))
        finally:
            await src.aclose()

    out = asyncio.run(run())
    assert out.identity == {"title": "ООО «РОМАШКА»", "inn": SUBJ, "ogrn": SUBJ_OGRN}
