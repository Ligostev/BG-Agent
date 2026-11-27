"""DaData на имитации API (ключа в облаке нет): клиент, разбор карточки, источники."""

import asyncio
import json

import httpx
import pytest

from app.sources import SourceContext, SourceData
from app.sources.bankrot import BankruptcySource
from app.sources.dadata import DaDataSource
from app.sources.fns import FnsSource
from app.sources.parties import related_parties, subject
from dadata import DaDataAuthError, DaDataClient, DaDataError, DaDataLimitError, parse_party

SUBJ = "7707083893"
OGRN = "1027700132195"
DIR = "500100732259"
FOUNDER = "7736050003"
IP_INN = "784806113663"

# Ответ findById/party на «Максимальном» тарифе (поля по руководству DaData).
FULL = {
    "value": "ООО \"РОМАШКА\"",
    "unrestricted_value": "ООО \"РОМАШКА\"",
    "data": {
        "inn": SUBJ, "kpp": "773601001", "ogrn": OGRN, "ogrn_date": 1029456000000,
        "type": "LEGAL",
        "name": {"full_with_opf": "ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ \"РОМАШКА\"",
                 "short_with_opf": "ООО \"РОМАШКА\""},
        "okved": "41.20",
        "okveds": [{"main": True, "code": "41.20", "name": "Строительство жилых и нежилых зданий"}],
        "opf": {"code": "12300", "full": "Общество с ограниченной ответственностью", "short": "ООО"},
        "management": {"name": "Иванов Иван Иванович", "post": "ГЕНЕРАЛЬНЫЙ ДИРЕКТОР",
                       "disqualified": True},
        "managers": [{"inn": DIR, "fio": {"surname": "Иванов", "name": "Иван",
                                          "patronymic": "Иванович"},
                      "post": "ГЕНЕРАЛЬНЫЙ ДИРЕКТОР", "type": "EMPLOYEE"}],
        "founders": [{"inn": FOUNDER, "ogrn": "1027700070518", "name": "ПАО \"ГАЗПРОМ\"",
                      "type": "LEGAL", "share": {"type": "PERCENT", "value": 60}},
                     {"inn": DIR, "fio": {"surname": "Иванов", "name": "Иван",
                                          "patronymic": "Иванович"},
                      "type": "PHYSICAL", "share": {"type": "FRACTION", "numerator": 2,
                                                    "denominator": 5}}],
        "state": {"status": "LIQUIDATING", "registration_date": 1029456000000,
                  "actuality_date": 1764201600000, "liquidation_date": None},
        "address": {"value": "г Москва, ул Вавилова, д 19",
                    "data": {"region_with_type": "г Москва", "source": "117312, МОСКВА, ВАВИЛОВА, 19"}},
        "capital": {"type": "УСТАВНЫЙ КАПИТАЛ", "value": 10000},
        "employee_count": 42,
        "finance": {"tax_system": "USN", "year": 2025, "income": 1500000.5, "expense": 1200000,
                    "debt": 3500, "penalty": 1000},
        "documents": {"smb": {"type": "SMB", "category": "MICRO"}},
        "licenses": [{"number": "1"}],
        "branch_count": 0,
        "invalid": None,
    },
}

# Тот же ответ на бесплатном тарифе: только базовые поля.
BASIC = {"value": "ООО \"РОМАШКА\"", "data": {
    "inn": SUBJ, "ogrn": OGRN, "type": "LEGAL",
    "name": {"short_with_opf": "ООО \"РОМАШКА\""},
    "management": {"name": "Иванов Иван Иванович", "post": "ГЕНЕРАЛЬНЫЙ ДИРЕКТОР"},
    "state": {"status": "ACTIVE"}, "address": {"value": "г Москва"},
}}

IP = {"value": "ИП Петров Пётр Петрович", "data": {
    "inn": IP_INN, "ogrn": "304500116000157", "type": "INDIVIDUAL",
    "fio": {"surname": "Петров", "name": "Пётр", "patronymic": "Петрович"},
    "name": {"full_with_opf": "Индивидуальный предприниматель Петров Пётр Петрович"},
    "state": {"status": "ACTIVE"},
}}


class FakeDaData:
    def __init__(self, answers=None, status=200):
        self.answers = answers or {}
        self.status = status if isinstance(status, list) else [status]
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append((request.url.path, body, dict(request.headers)))
        status = self.status.pop(0) if len(self.status) > 1 else self.status[0]
        if status != 200:
            return httpx.Response(status, text="error")
        found = self.answers.get(body["query"])
        return httpx.Response(200, json={"suggestions": [found] if found else []})


def make_client(site, **kw) -> DaDataClient:
    kw.setdefault("api_key", "test-key")
    return DaDataClient(transport=httpx.MockTransport(site), retries=kw.pop("retries", 0), **kw)


def run(coro):
    return asyncio.run(coro)


def test_client_sends_key_and_caches():
    site = FakeDaData({SUBJ: FULL})

    async def go():
        c = make_client(site, secret="sec")
        try:
            a = await c.find_party(SUBJ, kind="LEGAL")
            b = await c.find_party(SUBJ, kind="LEGAL")
            return a, b
        finally:
            await c.aclose()

    a, b = run(go())
    assert a == b == FULL
    assert len(site.calls) == 1  # второй раз из кеша
    path, body, headers = site.calls[0]
    assert path.endswith("/findById/party")
    assert body == {"query": SUBJ, "count": 1, "branch_type": "MAIN", "type": "LEGAL"}
    assert headers["authorization"] == "Token test-key" and headers["x-secret"] == "sec"


def test_client_not_found_and_errors():
    async def go(site, **kw):
        c = make_client(site, **kw)
        try:
            return await c.find_party(SUBJ)
        finally:
            await c.aclose()

    assert run(go(FakeDaData())) is None
    with pytest.raises(DaDataAuthError):
        run(go(FakeDaData(status=403)))
    with pytest.raises(DaDataLimitError):
        run(go(FakeDaData(status=429)))
    with pytest.raises(DaDataError):
        run(go(FakeDaData(status=500)))
    with pytest.raises(DaDataAuthError):  # без ключа запрос не уходит
        run(go(FakeDaData(), api_key=""))
    # одна повторная попытка после сбоя сервера
    assert run(go(FakeDaData({SUBJ: BASIC}, status=[502, 200]), retries=1)) == BASIC


def test_parse_full_card():
    card = parse_party(FULL)
    rows = dict(card.summary)
    assert card.identity == {"kind": "ul", "title": "ООО \"РОМАШКА\"", "inn": SUBJ, "ogrn": OGRN}
    assert rows["Статус"] == "Ликвидируется"
    assert rows["ОГРН"] == f"{OGRN} от 16.08.2002"
    assert rows["ОКВЭД"] == "41.20 Строительство жилых и нежилых зданий"
    assert rows["Уставный капитал"] == "10 000 ₽"
    assert rows["Налоговый режим"] == "УСН"
    assert rows["Доходы за 2025"] == "1 500 000 ₽"
    assert rows["Недоимки по налогам за 2025"] == "3 500 ₽"
    assert rows["Реестр МСП"] == "микропредприятие"
    assert rows["Учредители"] == "Иванов Иван Иванович (2/5); ПАО \"ГАЗПРОМ\" (60%)"
    assert card.summary[0][0] == "Внимание"
    assert set(card.flags) == {"Ликвидируется", "Дисквалифицированные лица в руководстве",
                               "Недоимки по налогам"}
    by_inn = {p["inn"]: p for p in card.parties}
    assert by_inn[DIR]["role"] == "Руководитель, Учредитель"
    assert by_inn[DIR]["post"] == "ГЕНЕРАЛЬНЫЙ ДИРЕКТОР" and by_inn[DIR]["share"] == "2/5"
    assert by_inn[FOUNDER]["kind"] == "company"
    assert len(card.parties) == 2  # руководитель из management не задвоился


def test_parse_basic_card_and_ip():
    card = parse_party(BASIC)
    assert card.flags == [] and "Внимание" not in dict(card.summary)
    assert card.parties == [{"inn": "", "name": "Иванов Иван Иванович", "role": "Руководитель",
                             "kind": "person", "share": None, "post": "ГЕНЕРАЛЬНЫЙ ДИРЕКТОР"}]
    ip = parse_party(IP)
    assert ip.kind == "ip" and ip.title == "ИП Петров Пётр Петрович"
    assert dict(ip.summary)["Тип"] == "Индивидуальный предприниматель"
    assert "ОГРНИП" in dict(ip.summary)


def test_source_card_and_ip_type():
    site = FakeDaData({SUBJ: FULL, IP_INN: IP})

    async def go(query):
        src = DaDataSource(make_client(site))
        try:
            return await src.fetch(SourceContext(query=query, ask_captcha=None))
        finally:
            await src.aclose()

    data = run(go(SUBJ))
    assert data.found and data.identity["inn"] == SUBJ
    assert data.data["flagged"] and "Ликвидируется" in data.data["verdict"]
    assert run(go(IP_INN)).identity["kind"] == "ip"
    assert site.calls[-1][1]["type"] == "INDIVIDUAL"
    assert run(go("1234567890")).found is False  # неверная контрольная сумма: запроса нет
    assert len(site.calls) == 2


def test_related_parties_merge_dadata_and_fns():
    from tests.test_bankrot import CARD, NONAME, OTHER

    dd = SourceData(found=True, identity={"inn": SUBJ, "title": "ООО \"РОМАШКА\" (DaData)"},
                    data={"parties": parse_party(FULL).parties})
    fns = SourceData(found=True, identity={"inn": SUBJ, "title": "ООО РОМАШКА"}, data=CARD)
    ctx = SourceContext(query=SUBJ, ask_captcha=None, previous={"dadata": dd, "fns": fns})
    assert subject(ctx)["title"] == "ООО \"РОМАШКА\" (DaData)"
    parties = related_parties(ctx, exclude={SUBJ})
    assert [p.inn for p in parties][:2] == [DIR, FOUNDER]  # DaData первой
    assert {p.inn for p in parties} == {DIR, FOUNDER, OTHER, NONAME}
    director = parties[0]
    assert "Руководитель" in director.roles and "Учредитель" in director.roles

    # руководитель без ИНН (бесплатный тариф) — только для проверок по ФИО
    basic = SourceData(found=True, identity={"inn": SUBJ}, data={"parties": parse_party(BASIC).parties})
    ctx = SourceContext(query=SUBJ, ask_captcha=None, previous={"dadata": basic})
    assert related_parties(ctx) == []
    assert [p.name for p in related_parties(ctx, with_inn=False)] == ["Иванов Иван Иванович"]


def test_bankruptcy_uses_dadata_without_fns():
    from tests.test_bankrot import FakeFedresurs, make_client as fed_client

    dd = SourceData(found=True, identity={"inn": SUBJ, "title": "ООО \"РОМАШКА\""},
                    data={"parties": parse_party(FULL).parties})

    async def go():
        src = BankruptcySource(fed_client(FakeFedresurs()))
        try:
            return await src.fetch(SourceContext(query=SUBJ, ask_captcha=None,
                                                 previous={"dadata": dd}))
        finally:
            await src.aclose()

    data = run(go())
    assert {r["inn"] for r in data.data["related"]} == {DIR, FOUNDER}
    assert data.data["flagged"]  # руководитель — банкрот в имитации ЕФРСБ


class _NoCallClient:
    solver = None

    async def lookup(self, query):
        raise AssertionError("pb.nalog.ru не должен вызываться")

    async def aclose(self):
        pass


def test_fns_modes():
    dd = SourceData(found=True, summary=[("Наименование", "x")], identity={"inn": SUBJ})
    ctx = SourceContext(query=SUBJ, ask_captcha=None, previous={"dadata": dd})
    assert run(FnsSource(_NoCallClient(), mode="fallback").fetch(ctx)).skipped
    assert run(FnsSource(_NoCallClient(), mode="off").fetch(ctx)).skipped
    with pytest.raises(ValueError):
        FnsSource(_NoCallClient(), mode="sometimes")


def test_fns_fallback_runs_when_dadata_failed_and_dedupes_when_found():
    from tests.test_client import FakeSite, make_client as pb_client

    async def go(previous, mode):
        src = FnsSource(pb_client(FakeSite()), mode=mode)
        try:
            return await src.fetch(SourceContext(query=OGRN, ask_captcha=None, previous=previous))
        finally:
            await src.aclose()

    full = run(go({}, "fallback"))  # DaData упала: в previous её нет
    assert full.found and not full.skipped and ("ИНН", SUBJ) in full.summary
    dd = SourceData(found=True, summary=[("Тип", "Юридическое лицо"), ("ИНН", SUBJ)])
    extra = run(go({"dadata": dd}, "always"))
    assert extra.found and all(k not in ("Тип", "ИНН") for k, _ in extra.summary)


def test_cabinet_dadata_first_and_skipped_source(tmp_path):
    from tests.test_app import make_client as make_app, register, wait_status

    dadata = DaDataSource(make_client(FakeDaData({SUBJ: FULL})))
    fns = FnsSource(_NoCallClient(), mode="fallback")
    with make_app(tmp_path, [dadata, fns]) as c:
        register(c)
        r = c.post("/checks", data={"query": SUBJ})
        check_id = int(r.url.path.rsplit("/", 1)[1])
        data = wait_status(c, check_id, {"done", "error"})
        assert data["status"] == "done"
        assert [(x["source"], x["status"]) for x in data["results"]] == [("dadata", "found")]
        assert data["title"] == "ООО \"РОМАШКА\"" and data["ogrn"] == OGRN
        page = c.get(f"/checks/{check_id}").text
        assert "Карточка компании (DaData)" in page and "Ликвидируется" in page


class _FailingClient(_NoCallClient):
    def __init__(self, exc):
        self.exc = exc

    async def lookup(self, query):
        raise self.exc


def test_fns_failure_is_soft_when_dadata_answered():
    import httpx as _httpx
    from app.captcha import CaptchaTimeout

    dd = SourceData(found=True, summary=[("ИНН", SUBJ)], identity={"inn": SUBJ})
    ctx = SourceContext(query=SUBJ, ask_captcha=None, previous={"dadata": dd})
    for exc, reason in ((CaptchaTimeout(), "капчу не ввели вовремя"),
                        (_httpx.ConnectError("refused"), "сайт недоступен"),
                        (RuntimeError("неожиданный ответ"), "ошибка сайта")):
        data = run(FnsSource(_FailingClient(exc)).fetch(ctx))
        assert data.found and data.data["unavailable"]
        assert reason in data.summary[0][1] and "DaData" in data.summary[0][1]
        assert data.identity == {}  # не перебивает название и ИНН из DaData
        assert related_parties(SourceContext(query=SUBJ, ask_captcha=None,
                                             previous={"dadata": dd, "fns": data})) == []

    # DaData не ответила: отказ pb.nalog.ru остаётся ошибкой источника
    with pytest.raises(RuntimeError):
        run(FnsSource(_FailingClient(RuntimeError("x"))).fetch(
            SourceContext(query=SUBJ, ask_captcha=None)))


def test_cabinet_done_with_dadata_when_pbnalog_refuses(tmp_path):
    from tests.test_app import make_client as make_app, register, wait_status
    from tests.test_bankrot import FakeFedresurs, make_client as fed_client

    dadata = DaDataSource(make_client(FakeDaData({SUBJ: FULL})))
    fns = FnsSource(_FailingClient(RuntimeError("403 Forbidden")))
    bankrot = BankruptcySource(fed_client(FakeFedresurs()))
    with make_app(tmp_path, [dadata, fns, bankrot]) as c:
        register(c)
        r = c.post("/checks", data={"query": SUBJ})
        check_id = int(r.url.path.rsplit("/", 1)[1])
        data = wait_status(c, check_id, {"done", "error"})
        assert data["status"] == "done" and data["title"] == "ООО \"РОМАШКА\""
        assert [(x["source"], x["status"]) for x in data["results"]] == [
            ("dadata", "found"), ("fns", "found"), ("bankrot", "found")]
        page = c.get(f"/checks/{check_id}").text
        assert "pb.nalog.ru не ответил" in page and "Источник ответил ошибкой" not in page
        related = c.get(f"/api/checks/{check_id}/data/bankrot").json()["related"]
        assert {r["inn"] for r in related} == {DIR, FOUNDER}  # связанные лица из DaData
