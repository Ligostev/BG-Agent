import asyncio
import base64
import json

import httpx
import pytest

from fssp import CaptchaRequired, FsspClient, parse_amount, parse_table
from fssp.client import BASE_URL


def row(debtor, number, ended, subject):
    cells = [debtor, number, "Судебный приказ от 01.01.2023 № 2-1/2023", ended,
             '<a href="#">Оплатить</a>', subject, "ОСП по ЦАО № 1", "ПЕТРОВ П. П."]
    return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"


def table(*rows, next_page=None):
    pager = f'<div class="pagination"><a href="#">1</a><a href="#">{next_page}</a></div>' if next_page else ""
    return ('<div class="results"><table><tr><th>Должник</th></tr>'
            '<tr class="region-title"><td colspan="8">г. Москва</td></tr>'
            + "".join(rows) + "</table>" + pager + "</div>")


ACTIVE = row('ООО "РОМАШКА"<br>123456, Г. МОСКВА', "12345/23/77001-ИП от 10.02.2023", "",
             "Задолженность по налогам: 1 234,56 руб. Исполнительский сбор: 1000 руб.")
ENDED = row('ООО "РОМАШКА"<br>123456, Г. МОСКВА', "999/21/77001-ИП от 10.02.2021",
            "15.06.2021, ст. 46 ч. 1 п. 3", "Штраф: 500 руб.")
CAPTCHA_IMG = base64.b64encode(b"\xff\xd8jpeg").decode()
CAPTCHA = (f'<form><img src="data:image/jpeg;base64,{CAPTCHA_IMG}" id="capchaVisual">'
           '<input name="code"></form>')


def test_parse_table_and_amount():
    rows = parse_table(table(ACTIVE, ENDED))
    assert len(rows) == 2
    assert rows[0].proceeding == "12345/23/77001-ИП от 10.02.2023"
    assert rows[0].active and not rows[1].active
    assert rows[0].amount == 2234.56
    assert rows[0].department == "ОСП по ЦАО № 1"
    assert rows[0].debtor_name == 'ООО "РОМАШКА"'
    assert rows[0].debtor == 'ООО "РОМАШКА" 123456, Г. МОСКВА'
    assert parse_amount("Иное взыскание неимущественного характера") is None


class FakeFssp:
    def __init__(self, captcha=False, jsonp=False):
        self.captcha = captcha
        self.jsonp = jsonp
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        p = dict(request.url.params)
        self.calls.append(p)
        assert p["is[variant]"] == "2" and p["is[drtr_name]"] == "РОМАШКА"
        if self.captcha and p.get("code") != "ж7к2":
            html = CAPTCHA
        elif p["page"] == "1":
            html = table(ACTIVE, next_page=2)
        else:
            html = table(ENDED)
        body = json.dumps({"data": html, "err": ""})
        return httpx.Response(200, text=f"jQuery123({body});" if self.jsonp else body)


def client(site, **kw):
    http = httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(site))
    return FsspClient(http=http, min_interval=0, **kw)


def test_search_legal_paginates_and_reads_jsonp():
    site = FakeFssp(jsonp=True)

    async def go():
        async with client(site) as fssp:
            return await fssp.search_legal("РОМАШКА")

    rows = asyncio.run(go())
    assert [r.active for r in rows] == [True, False]
    assert [c["page"] for c in site.calls] == ["1", "2"]


def test_captcha_is_solved():
    site = FakeFssp(captcha=True)
    answers = iter(["неверно", "ж7к2", "ж7к2"])

    async def solver(image: bytes) -> str:
        assert image == b"\xff\xd8jpeg"
        return next(answers)

    async def go():
        async with client(site, solver=solver) as fssp:
            return await fssp.search_legal("РОМАШКА")

    assert len(asyncio.run(go())) == 2
    assert [c.get("code") for c in site.calls] == [None, "неверно", "ж7к2", None, "ж7к2"]


def test_captcha_without_solver_raises():
    async def go():
        async with client(FakeFssp(captcha=True)) as fssp:
            await fssp.search_legal("РОМАШКА")

    with pytest.raises(CaptchaRequired):
        asyncio.run(go())
