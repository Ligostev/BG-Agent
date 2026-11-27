"""Блокировки счетов (ФНС bi.do) и риск ЦБ (ЗСК) на имитации сайтов: из облака они недоступны."""

import asyncio
from urllib.parse import parse_qs

import httpx
import pytest

from accblock import (AccountBlockReport, CaptchaRequired, FnsBlocksClient, FnsFormatError,
                      FnsResult, ZskClient, ZskResult, parse_decisions, parse_zsk)
from accblock.zsk import find_endpoints
from app.captcha import CaptchaTimeout
from app.sources import SourceContext, SourceData
from app.sources.accblock import AccountBlocksSource

INN = "7707083893"
TOKEN = "ABCDEF0123456789ABCDEF"
ROW = {"NOMER": "1234", "DATA": "01.11.2026", "IFNS": "7701", "BIK": "044525225",
       "NAIMBANK": "ПАО СБЕРБАНК", "DATABI": "01.11.2026 10:00:00"}


class FakeFns:
    """Без капчи сразу отвечает, с need_captcha — сначала просит капчу."""

    def __init__(self, rows=None, need_captcha=False, answer="12345", status=200, body=None):
        self.rows = [ROW] if rows is None else rows
        self.need_captcha = need_captcha
        self.answer = answer
        self.status = status
        self.body = body
        self.posts: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/bi.do":
            return httpx.Response(200, text="<html>bi</html>")
        if path == "/static/captcha.html":
            return httpx.Response(200, text=TOKEN)
        if path == "/static/captcha.bin":
            assert request.url.params["a"] == TOKEN
            return httpx.Response(200, content=b"GIF89a")
        if path == "/bi2-proc.json":
            form = {k: v[0] if v else "" for k, v in
                    parse_qs(request.content.decode(), keep_blank_values=True).items()}
            self.posts.append(form)
            assert form["requestType"] == "FINDPRS" and form["innPRS"] == INN
            if self.status != 200:
                return httpx.Response(self.status, text="error")
            if self.need_captcha and form["captcha"] != self.answer:
                return httpx.Response(400, json={"ERRORS": {"captcha": ["Требуется ввести цифры с картинки"]}})
            if self.body is not None:
                return httpx.Response(200, json=self.body)
            return httpx.Response(200, json={"formName": "prs", "datePRS": "27.11.2026",
                                             "rows": self.rows})
        return httpx.Response(404)


def fns_client(site) -> FnsBlocksClient:
    return FnsBlocksClient(min_interval=0, http=httpx.AsyncClient(
        base_url="https://service.nalog.ru", transport=httpx.MockTransport(site)))


def zsk_client(handler, url="https://www.cbr.ru/zsk/check") -> ZskClient:
    return ZskClient(url=url, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def ctx(answer="12345", previous=None, query=INN):
    shown = []

    async def ask(image: bytes) -> str:
        shown.append(image)
        if answer is None:
            raise CaptchaTimeout
        return answer
    c = SourceContext(query=query, ask_captcha=ask, previous=previous or {})
    return c, shown


def test_parse_decisions_fields_and_empty():
    d = parse_decisions({"rows": [ROW]})[0]
    assert (d.number, d.date, d.tax_office, d.bik, d.bank) == ("1234", "01.11.2026", "7701",
                                                                "044525225", "ПАО СБЕРБАНК")
    assert "№ 1234 от 01.11.2026" in d.label()
    assert parse_decisions({"rows": []}) == []
    assert parse_decisions({"message": "Действующих решений о приостановлении нет"}) == []
    with pytest.raises(FnsFormatError):  # непонятный ответ — не «решений нет»
        parse_decisions({"foo": "bar"})
    with pytest.raises(CaptchaRequired):
        parse_decisions({"ERRORS": {"captcha": ["Неверно указаны символы"]}})


def test_client_asks_captcha_and_retries():
    site = FakeFns(need_captcha=True)
    seen = []

    async def solver(image):
        seen.append(image)
        return "12345"

    async def run():
        async with fns_client(site) as c:
            return await c.decisions(INN, solver=solver)
    out = asyncio.run(run())
    assert [d.number for d in out] == ["1234"]
    assert seen == [b"GIF89a"]
    assert site.posts[-1]["captchaToken"] == TOKEN and site.posts[-1]["bikPRS"] == "044525225"


def test_client_without_solver_raises_captcha():
    async def run():
        async with fns_client(FakeFns(need_captcha=True)) as c:
            return await c.decisions(INN)
    with pytest.raises(CaptchaRequired):
        asyncio.run(run())


def test_parse_zsk_phrases_and_flags():
    assert parse_zsk({"isHighRisk": True})[0] is True
    assert parse_zsk("<p>Организация не отнесена к группе высокого уровня риска.</p>")[0] is False
    high, text = parse_zsk("<div>ИНН 7707083893 отнесен к группе высокого уровня риска.</div>")
    assert high is True and "высокого уровня риска" in text
    assert parse_zsk("Сервис временно недоступен")[0] is None


def test_find_endpoints():
    html = '<form action="/zsk/check" method="get"></form><script>fetch("/api/zsk/inn")</script>'
    assert find_endpoints(html) == ["/zsk/check", "/api/zsk/inn"]


def test_report_texts():
    clean = AccountBlockReport(INN, FnsResult(True), ZskResult(True, False))
    assert not clean.flagged and not clean.unavailable
    assert clean.verdict == "Действующих решений о приостановлении операций по счетам нет"
    blocked = AccountBlockReport(INN, FnsResult(True, parse_decisions({"rows": [ROW]})),
                                 ZskResult(False, error="не настроен"))
    assert blocked.flagged and blocked.verdict.startswith("Счета заблокированы")
    assert any("п. 12 ст. 76 НК РФ" in v for _, v in blocked.summary())
    assert any("cbr.ru" in v for _, v in blocked.summary())


def test_source_found_blocks_and_zsk():
    src = AccountBlocksSource(fns=fns_client(FakeFns()),
                              zsk=zsk_client(lambda r: httpx.Response(
                                  200, json={"isHighRisk": False})))
    c, shown = ctx()
    data = asyncio.run(src.fetch(c))
    assert data.found and data.data["flagged"] and not data.data["unavailable"]
    assert data.data["fns"]["decisions"][0]["number"] == "1234"
    assert data.data["zsk"]["high_risk"] is False
    assert shown == []  # без капчи картинку не показываем


def test_source_uses_inn_from_card_when_query_is_ogrn():
    site = FakeFns(rows=[])
    src = AccountBlocksSource(fns=fns_client(site), zsk=ZskClient(url=""))
    prev = {"dadata": SourceData(found=True, identity={"inn": INN, "title": "ООО"})}
    c, _ = ctx(previous=prev, query="1027700132195")
    data = asyncio.run(src.fetch(c))
    assert data.data["verdict"].startswith("Действующих решений") and not data.data["flagged"]
    assert any("Проверить вручную" in v for _, v in data.summary)


@pytest.mark.parametrize("site,answer,reason", [
    (FakeFns(status=503), "1", "сайт недоступен"),
    (FakeFns(need_captcha=True), None, "капчу не ввели вовремя"),
    (FakeFns(body={"strange": 1}), "1", "ответ сервиса не удалось разобрать"),
])
def test_source_does_not_fail_when_fns_unavailable(site, answer, reason):
    src = AccountBlocksSource(fns=fns_client(site), zsk=ZskClient(url=""))
    c, _ = ctx(answer=answer)
    data = asyncio.run(src.fetch(c))
    assert data.found and data.data["unavailable"] and not data.data["flagged"]
    assert data.data["fns"]["error"] == reason
    assert data.data["verdict"].startswith("Не удалось проверить")
    assert "service.nalog.ru/bi.do" in data.summary[0][1]


def test_source_without_inn():
    src = AccountBlocksSource(fns=fns_client(FakeFns()), zsk=ZskClient(url=""))
    c, _ = ctx(query="ООО Ромашка")
    data = asyncio.run(src.fetch(c))
    assert not data.found and "Нет ИНН" in data.summary[0][1]


def test_zsk_error_does_not_break_fns_result():
    src = AccountBlocksSource(fns=fns_client(FakeFns(rows=[])),
                              zsk=zsk_client(lambda r: httpx.Response(500)))
    c, _ = ctx()
    data = asyncio.run(src.fetch(c))
    assert data.data["fns"]["checked"] and not data.data["zsk"]["checked"]
    assert not data.data["unavailable"]
