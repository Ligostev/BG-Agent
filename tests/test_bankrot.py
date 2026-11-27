"""Проверка банкротства на имитации ЕФРСБ и Федресурса (живые сайты из облака недоступны)."""

import asyncio

import httpx

from app.sources import SourceContext, SourceData
from app.sources.bankrot import BankruptcySource
from bankrot import FedresursClient, Party, check_bankruptcy, extract_parties
from bankrot.client import parse_bankruptcy

SUBJ = "7707083893"      # проверяемая компания
DIR = "500100732259"     # руководитель, физлицо-банкрот
FOUNDER = "7736050003"   # учредитель-компания, не банкрот
OTHER = "7702070139"     # другая компания руководителя, банкрот (дело завершено)
NONAME = "784806113663"  # физлицо, на котором имитация сайта падает

CARD = {
    "hit": {"inn": SUBJ, "namec": "ООО РОМАШКА", "ogrn": "1027700132195"},
    "card": {
        "masruk": [{"name": "Иванов Иван Иванович", "inn": DIR, "position": "Генеральный директор",
                    "companies": [{"namec": "ООО ВАСИЛЕК", "inn": OTHER}]}],
        "uchr": [{"namec": "ПАО ГАЗПРОМ", "inn": FOUNDER, "share": "100%"},
                 {"fio": "Петров П.П.", "inn": NONAME},
                 {"fio": "Без ИНН"}],
        "vyp": {"ИНН": SUBJ},
    },
}


class FakeFedresurs:
    def __init__(self, fail_on=()):
        self.fail_on = set(fail_on)
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Referer"].endswith("fedresurs.ru/")
        host, path, q = request.url.host, request.url.path, request.url.params.get("searchString")
        self.calls.append((host, path, q))
        if q in self.fail_on:
            return httpx.Response(503)
        if host == "bankrot.fedresurs.ru" and path == "/backend/prsnbankrupts":
            rows = []
            if q == DIR:
                rows = [{"guid": "p-1", "fio": "Иванов Иван Иванович", "inn": DIR,
                         "lastLegalCase": {"number": "А40-1/2025",
                                           "status": {"description": "Реализация имущества гражданина"}}},
                        {"guid": "p-2", "fio": "Иванов Иван Иванович", "inn": "500100000000"}]
            return httpx.Response(200, json={"pageData": rows, "total": len(rows)})
        if host == "bankrot.fedresurs.ru" and path == "/backend/cmpbankrupts":
            rows = []
            if q == OTHER:
                rows = [{"guid": "c-2", "name": "ООО ВАСИЛЕК", "inn": OTHER,
                         "lastLegalCase": {"number": "А40-2/2020",
                                           "status": {"description": "Конкурсное производство завершено"}}}]
            return httpx.Response(200, json={"pageData": rows, "total": len(rows)})
        if host == "fedresurs.ru" and path == "/backend/companies":
            return httpx.Response(200, json={"pageData": [{"guid": "g-subj", "inn": q}]})
        if host == "fedresurs.ru" and path == "/backend/companies/g-subj/publications":
            return httpx.Response(200, json={"pageData": [
                {"guid": "m1", "type": "Уведомление о намерении кредитора обратиться в суд с заявлением о банкротстве",
                 "datePublish": "2026-11-01T10:00:00"},
                {"guid": "m2", "type": "Сведения о чистых активах", "datePublish": "2026-04-01T10:00:00"}]})
        return httpx.Response(404)


def make_client(site) -> FedresursClient:
    return FedresursClient(http=httpx.AsyncClient(transport=httpx.MockTransport(site)),
                           min_interval=0, retries=0)


def test_extract_parties_roles_and_nested_companies():
    parties = {p.inn: p for p in extract_parties(CARD, exclude={SUBJ})}
    assert set(parties) == {DIR, FOUNDER, OTHER, NONAME}
    assert parties[DIR].roles == ["Руководитель"] and parties[DIR].kind == "person"
    assert parties[FOUNDER].roles == ["Учредитель"] and parties[FOUNDER].name == "ПАО ГАЗПРОМ"
    assert parties[OTHER].role == "Компания: руководитель Иванов Иван Иванович"
    assert len(extract_parties(CARD, exclude={SUBJ}, limit=2)) == 2


def test_parse_bankruptcy_status_variants():
    b = parse_bankruptcy({"name": "X", "lastLegalCase": {"status": {"description": "Наблюдение"}}}, "company")
    assert b.active and b.describe() == "Наблюдение"
    b = parse_bankruptcy({"name": "X", "status": "Производство по делу прекращено"}, "company")
    assert not b.active
    assert parse_bankruptcy({"lastName": "Иванов", "firstName": "Иван", "guid": "g"}, "person").url \
        == "https://fedresurs.ru/person/g"


def test_check_company_with_related():
    site = FakeFedresurs(fail_on={NONAME})

    async def go():
        async with make_client(site) as c:
            related = extract_parties(CARD, exclude={SUBJ})
            return await check_bankruptcy(c, Party(SUBJ, "ООО РОМАШКА"), related)

    report = asyncio.run(go())
    assert report.flagged and not report.subject.bankrupt
    assert len(report.intentions) == 1
    by_inn = {r.party.inn: r for r in report.related}
    # из двух Ивановых в выдаче берём только точное совпадение по ИНН
    assert [b.case_number for b in by_inn[DIR].records] == ["А40-1/2025"]
    assert by_inn[DIR].records[0].active
    assert not by_inn[OTHER].records[0].active
    assert not by_inn[FOUNDER].bankrupt and by_inn[NONAME].error
    summary = dict(report.summary())
    assert summary["Итог"] == "Есть признаки банкротства"
    assert summary["Компания"] == "Сведений о банкротстве нет"
    assert summary["Намерения кредиторов"] == "1 сообщ., последнее 2026-11-01"
    assert summary["Связанные лица"] == "Проверено 3, банкротства: 2"
    assert summary[f"Руководитель: Иванов Иван Иванович (ИНН {DIR})"] == \
        "Банкрот: Реализация имущества гражданина, дело А40-1/2025"
    assert "Петров" in summary["Не удалось проверить"]
    assert report.to_dict()["related"][0]["bankruptcies"][0]["url"] == "https://fedresurs.ru/person/p-1"


def test_source_for_person_without_fns_card():
    site = FakeFedresurs()

    async def go():
        source = BankruptcySource(make_client(site))
        try:
            ctx = SourceContext(query=DIR, ask_captcha=None,
                                previous={"fns": SourceData(found=False)})
            return await source.fetch(ctx)
        finally:
            await source.aclose()

    data = asyncio.run(go())
    assert data.found and data.data["flagged"]
    assert data.identity == {"title": "Иванов Иван Иванович", "inn": DIR}
    summary = dict(data.summary)
    assert "Намерения кредиторов" not in summary  # для физлиц не ищем
    assert summary["Физлицо / ИП"].startswith("Банкрот:")
    # у физлица без карточки ФНС связанных лиц нет, лишних запросов тоже
    assert [c[1] for c in site.calls] == ["/backend/prsnbankrupts"]


def test_source_uses_fns_card():
    site = FakeFedresurs()
    fns = SourceData(found=True, data=CARD, identity={"inn": SUBJ, "title": "ООО РОМАШКА",
                                                      "ogrn": "1027700132195"})

    async def go():
        source = BankruptcySource(make_client(site))
        try:
            return await source.fetch(SourceContext(query=SUBJ, ask_captcha=None,
                                                    previous={"fns": fns}))
        finally:
            await source.aclose()

    data = asyncio.run(go())
    assert data.identity == {}
    assert {r["inn"] for r in data.data["related"]} == {DIR, FOUNDER, OTHER, NONAME}


def test_subject_error_propagates():
    async def go():
        async with make_client(FakeFedresurs(fail_on={SUBJ})) as c:
            await check_bankruptcy(c, Party(SUBJ, "X"))

    try:
        asyncio.run(go())
    except Exception as e:
        assert "503" in str(e)
    else:
        raise AssertionError("ошибка по самому контрагенту должна пробрасываться")


def test_cabinet_shows_bankruptcy(tmp_path):
    from tests.test_app import FakeFns, make_client as make_app, register, wait_status

    source = BankruptcySource(make_client(FakeFedresurs()))
    with make_app(tmp_path, [FakeFns(), source]) as c:
        register(c)
        r = c.post("/checks", data={"query": SUBJ})
        check_id = int(r.url.path.rsplit("/", 1)[1])
        data = wait_status(c, check_id, {"done", "error"})
        assert [(x["source"], x["status"]) for x in data["results"]] == [("fns", "found"), ("bankrot", "found")]
        page = c.get(f"/checks/{check_id}").text
        assert "Банкротство (ЕФРСБ)" in page and "Есть признаки банкротства" in page
        assert c.get(f"/api/checks/{check_id}/data/bankrot").json()["creditor_intentions"]
