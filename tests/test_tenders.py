"""Подбор закупок: разбор извещений, сервис ЕИС (подменённый), правила отбора."""

import asyncio
import io
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest

from tenders import EisClient, EisError, Profile, match_notice, parse_notice
from tenders.eis import build_region_request, parse_response
from tenders.imports import load_contracts
from tenders.match import parse_codes, parse_regions, profile_from_contracts

XML = (Path(__file__).parent / "fixtures" / "epNotificationEF2020.xml").read_bytes()
NOW = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)


def test_parse_notice():
    n = parse_notice(XML, region="77")
    assert n.purchase_number == "0373200000126000123"
    assert n.doc_type == "epNotificationEF2020"
    assert n.subject.startswith("Капитальный ремонт кровли")
    assert n.customer == "ГБОУ ШКОЛА № 1" and n.customer_inn == "7701000001"
    assert n.placing_way == "Электронный аукцион" and "Сбербанк" in n.etp
    assert n.max_price == 12_500_000
    assert n.application_guarantee == 125_000
    assert n.contract_guarantee == 625_000        # 5 % от НМЦК, суммы в документе нет
    assert n.warranty_guarantee == 62_500
    assert n.okpd2 == ["43.91.19.110", "43.99.90.190"]
    assert n.ends_at.isoformat() == "2026-11-30T09:00:00+03:00"
    assert type(n).from_dict(n.to_dict()) == n


def test_parse_notice_skips_other_documents():
    assert parse_notice(b"<export><epCancel2020><commonInfo/></epCancel2020></export>") is None


def test_request_keeps_element_order():
    body = build_region_request("77", "epNotificationEF2020", date(2026, 11, 18), request_id="X",
                                now=datetime(2026, 11, 19, 5, 30))
    order = [body.index(t) for t in ("<orgRegion>77", "<subsystemType>PRIZ", "<documentType44>epNotificationEF2020",
                                     "<exactDate>2026-11-18")]
    assert order == sorted(order)
    assert "<individualPerson_token>{token}</individualPerson_token>" in body


def test_parse_response():
    ok = b"""<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>
      <ns2:getDocsByOrgRegionResponse xmlns:ns2="http://zakupki.gov.ru/fz44/get-docs-ip/ws">
      <dataInfo><archiveUrl>https://int44.zakupki.gov.ru/a/1</archiveUrl></dataInfo>
      </ns2:getDocsByOrgRegionResponse></soap:Body></soap:Envelope>"""
    assert parse_response(ok) == ["https://int44.zakupki.gov.ru/a/1"]
    assert parse_response(b"<Envelope><Body><r><dataInfo><noData>true</noData></dataInfo></r></Body></Envelope>") == []
    with pytest.raises(EisError, match="token"):
        parse_response(b"<Envelope><Body><Fault><faultstring>bad token</faultstring></Fault></Body></Envelope>")
    with pytest.raises(EisError, match="лимит"):
        parse_response("<Envelope><Body><r><errorInfo><code>1</code><message>лимит</message></errorInfo></r></Body></Envelope>".encode())


def make_zip(*xmls: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i, x in enumerate(xmls):
            zf.writestr(f"doc{i}.xml", x)
    return buf.getvalue()


def fake_eis(seen: list) -> httpx.AsyncClient:
    archive = make_zip(XML, b"<export><epCancel2020/></export>", b"not xml")

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            if b"epNotificationEF2020" not in request.content:
                return httpx.Response(200, content=b"<E><B><r><noData>true</noData></r></B></E>")
            return httpx.Response(200, content=b"<E><B><r><dataInfo><archiveUrl>https://eis/a/1</archiveUrl>"
                                                b"</dataInfo></r></B></E>")
        if len([r for r in seen if r.method == "GET"]) == 1:
            return httpx.Response(404, content=b"not ready")     # архив ещё готовится
        return httpx.Response(200, content=archive)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_client_downloads_and_parses():
    seen = []

    async def run():
        c = EisClient("secret-token", min_interval=0, download_delay=0, http=fake_eis(seen))
        out = [n async for n in c.notices("77", date(2026, 11, 18))]
        await c.aclose()
        return out

    notices = asyncio.run(run())
    assert [n.purchase_number for n in notices] == ["0373200000126000123"]
    assert notices[0].region == "77"
    posts = [r for r in seen if r.method == "POST"]
    assert len(posts) == 3 and all(b"secret-token" in r.content for r in posts)
    assert all(r.headers["individualPerson_token"] == "secret-token" for r in seen if r.method == "GET")


def test_client_requires_token():
    with pytest.raises(EisError):
        EisClient("")


def test_match_rules():
    n = parse_notice(XML, region="77")
    p = Profile(okpd2=["43.91"], regions=["77"], price_min=1_000_000, price_max=20_000_000)
    m = match_notice(n, p, NOW)
    assert m and "ОКПД2 43.91" in m.reasons and "регион: Москва" in m.reasons
    products = dict(m.products)
    assert products["БГ на исполнение контракта"] == 625_000
    assert products["БГ на участие (обеспечение заявки)"] == 125_000
    assert "страхование СМР (если требует контракт)" in products

    assert match_notice(n, Profile(okpd2=["43"], regions=["50"]), NOW) is None        # другой регион
    assert match_notice(n, Profile(okpd2=["41"]), NOW) is None                         # другой код
    assert match_notice(n, Profile(okpd2=["4"]), NOW) is None
    assert match_notice(n, Profile(okpd2=["43"], price_max=10_000_000), NOW) is None   # дороже
    assert match_notice(n, Profile(okpd2=["43"], min_days_left=20), NOW) is None       # не успеть
    assert match_notice(n, Profile(keywords=["кровл"]), NOW) is not None
    assert match_notice(n, Profile(), NOW) is None


def test_parse_profile_fields():
    assert parse_codes("43.21, 43.22.11.000; 41 и 43.21") == ["43.21", "43.22.11.000", "41"]
    assert parse_regions("77, 50 7 и 999") == ["77", "50", "07"]


def test_profile_from_contracts_csv():
    csv = ("Реестровый номер контракта;Поставщик;ИНН поставщика;Цена контракта;ОКПД2;Регион\n"
           "2770100000126000001;ООО Кровля;7700000000;3 000 000,00;43.91.19.110;Москва\n"
           "2770100000126000002;ООО Кровля;7700000000;8 000 000,00;43.91.11.000, 43.99.10;Московская область\n")
    contracts = load_contracts(csv.encode(), "export.csv")
    assert [c.price for c in contracts] == [3_000_000, 8_000_000]
    p = profile_from_contracts(contracts)
    assert p.okpd2[0] == "43.91" and "43.99" in p.okpd2
    assert p.regions == ["77", "50"]
    assert (p.price_min, p.price_max) == (1_500_000, 16_000_000)


def test_load_contracts_rejects_unknown_table():
    with pytest.raises(ValueError):
        load_contracts("a;b\n1;2\n".encode(), "x.csv")
