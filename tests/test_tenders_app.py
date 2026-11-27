"""Страница «Закупки» в кабинете и загрузка извещений в базу."""

import re

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app.models import Deal, TenderNotice
from app.tenders_sync import sync
from tenders import parse_notice

from test_app import FakeFns, make_client, register

XML = (Path(__file__).parent / "fixtures" / "epNotificationEF2020.xml").read_bytes()


class FakeEis:
    def __init__(self):
        self.calls = []

    async def notices(self, region, day, laws):
        self.calls.append((region, day, laws))
        if region == "77":
            n = parse_notice(XML, region=region)
            n.ends_at = datetime.now(timezone.utc) + timedelta(days=10)   # чтобы тест не устарел
            yield n


def test_profile_sync_matches_and_guarantee(tmp_path):
    with make_client(tmp_path, [FakeFns()]) as c:
        register(c)
        page = c.get("/tenders").text
        assert "Загрузка из ЕИС не настроена" in page and "Заполните коды ОКПД2" in page

        r = c.post("/tenders/profile", data={"okpd2": "43.91, 43.99", "regions": "77, 50",
                                             "keywords": "крыша, -проект", "laws": ["44-ФЗ", "615-ПП"],
                                             "price_min": "1 000 000", "price_max": "", "min_days_left": "0"})
        assert r.url.path == "/tenders"

        eis = FakeEis()
        sm = c.app.state.sessionmaker
        stats = c.portal.call(sync, sm, eis, date(2026, 11, 18))
        assert stats == {"50": 0, "77": 1}
        assert eis.calls[0][2] == ("44-ФЗ", "615-ПП")
        # Повторная загрузка того же извещения не создаёт дубль.
        c.portal.call(sync, sm, eis, date(2026, 11, 18))

        page = c.get("/tenders").text
        assert "0373200000126000123" in page and "Капитальный ремонт кровли" in page
        assert "625 000 ₽" in page and "ОКПД2 43.91" in page and "слова: крыша" in page
        boxes = dict(re.findall(r'name="laws" value="([^"]+)"\s*(checked)?', page))
        assert boxes == {"44-ФЗ": "checked", "223-ФЗ": "", "615-ПП": "checked"}

        c.post("/tenders/0373200000126000123/guarantee")
        c.post("/tenders/0373200000126000123/guarantee")      # второй клик не дублирует заявку
        assert c.post("/tenders/1/guarantee").status_code == 404

        async def counts():
            from sqlalchemy import func, select
            async with sm() as db:
                n = await db.scalar(select(func.count()).select_from(TenderNotice))
                reqs = (await db.execute(select(Deal))).scalars().all()
                return n, sorted(r.product for r in reqs)

        n, products = c.portal.call(counts)
        assert n == 1
        assert products == ["БГ на гарантийные обязательства", "БГ на исполнение контракта",
                            "БГ на участие (обеспечение заявки)"]
        assert "Заявки на гарантию" in c.get("/tenders").text


def test_profile_import_from_contracts(tmp_path):
    csv = ("Реестровый номер контракта;Цена контракта;ОКПД2;Регион\n"
           "2770100000126000001;3 000 000,00;43.91.19.110;Москва\n").encode()
    with make_client(tmp_path, [FakeFns()]) as c:
        register(c)
        r = c.post("/tenders/profile/import", files={"file": ("export.csv", csv, "text/csv")})
        assert r.status_code == 200 and "по 1 контрактам" in r.text
        assert 'value="43.91"' in r.text and 'value="77"' in r.text
        bad = c.post("/tenders/profile/import", files={"file": ("x.csv", b"a;b\n1;2\n", "text/csv")})
        assert bad.status_code == 400 and "Не получилось разобрать" in bad.text


def test_tenders_need_login(tmp_path):
    with make_client(tmp_path, [FakeFns()]) as c:
        assert c.get("/tenders").url.path == "/login"


class FakeEmbedder:
    """Вектор «про кровлю / про что-то другое» вместо настоящей модели."""

    def __init__(self):
        self.calls = []

    def embed(self, texts):
        self.calls.append(texts)
        return [[1.0, 0.0] if ("кров" in t.lower() or "крыш" in t.lower()) else [0.0, 1.0] for t in texts]


def test_semantic_profile(tmp_path):
    from app.tenders_sync import embed_pending
    settings_kw = {"embed_model": "fake", "embed_threshold": 0.8}
    with make_client(tmp_path, [FakeFns()], **settings_kw) as c:
        register(c)
        c.post("/tenders/profile", data={"description": "Ремонт крыш и гидроизоляция", "regions": "77",
                                         "min_days_left": "0"})
        page = c.get("/tenders").text
        assert "Описание учтём в течение 15 минут" in page

        sm = c.app.state.sessionmaker
        c.portal.call(sync, sm, FakeEis(), date(2026, 11, 18))
        emb = FakeEmbedder()
        done = c.portal.call(embed_pending, sm, lambda: emb)
        assert done == {"profiles": 1, "notices": 1}
        # Повторный запуск ничего не считает и даже не загружает модель.
        assert c.portal.call(embed_pending, sm, lambda: (_ for _ in ()).throw(AssertionError("модель не нужна"))) \
            == {"profiles": 0, "notices": 0}

        page = c.get("/tenders").text
        assert "0373200000126000123" in page and "по смыслу: 100%" in page
        assert "Описание учтём" not in page


def test_semantic_off_message(tmp_path):
    with make_client(tmp_path, [FakeFns()]) as c:
        register(c)
        assert "Подбор по смыслу на сервере пока не включён" in c.get("/tenders").text
