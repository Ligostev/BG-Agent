"""Раздел CRM: роли, воронка сделок, банки, карточка клиента."""

import re
from datetime import date

from app.roles import set_role
from app.tenders_sync import sync

from test_app import INN, FakeFns, make_client, register, wait_status
from test_tenders_app import FakeEis

ADMIN = "boss@example.com"


def login(c, email):
    c.post("/logout")
    r = c.post("/login", data={"email": email, "password": "password1"})
    assert r.status_code == 200
    return r


def test_crm_only_for_staff(tmp_path):
    with make_client(tmp_path, [FakeFns()], admin_emails=(ADMIN,)) as c:
        assert c.get("/crm/deals").url.path == "/login"
        register(c)
        assert c.get("/crm/deals").status_code == 403
        assert "Сделки" not in c.get("/checks").text
        c.post("/logout")
        r = c.post("/register", data={"email": ADMIN, "password": "password1"})
        assert r.url.path == "/crm/deals"
        assert 'href="/crm/clients"' in r.text

        # Менеджера назначает администратор командой; после входа ему открыта CRM.
        assert c.portal.call(set_role, c.app.state.sessionmaker, "A@example.com", "manager")
        assert not c.portal.call(set_role, c.app.state.sessionmaker, "nobody@example.com", "manager")
        assert login(c, "a@example.com").url.path == "/crm/deals"


def test_guarantee_request_becomes_deal(tmp_path):
    with make_client(tmp_path, [FakeFns()], admin_emails=(ADMIN,)) as c:
        register(c)
        c.post("/tenders/profile", data={"okpd2": "43.91", "regions": "77", "min_days_left": "0"})
        c.portal.call(sync, c.app.state.sessionmaker, FakeEis(), date(2026, 11, 18))
        c.post("/tenders/0373200000126000123/guarantee")
        assert "Новая" in c.get("/tenders").text            # клиент видит этап своей заявки

        c.post("/logout")
        c.post("/register", data={"email": ADMIN, "password": "password1"})
        board = c.get("/crm/deals").text
        assert "0373200000126000123" in board and "a@example.com" in board
        deal_id = int(re.search(r'/crm/deals/(\d+)">№\d+ · БГ на исполнение', board).group(1))

        # Банк: добавление двигает сделку в «В банках», одобрение — в «Одобрена».
        c.post(f"/crm/deals/{deal_id}/banks", data={"bank": "Сбербанк"})
        c.post(f"/crm/deals/{deal_id}/banks", data={"bank": "сбербанк"})     # без дубля
        page = c.get(f"/crm/deals/{deal_id}").text
        assert page.count("<td>Сбербанк</td>") == 1 and "В банках" in page
        bank_id = int(re.search(rf'/crm/deals/{deal_id}/banks/(\d+)', page).group(1))
        c.post(f"/crm/deals/{deal_id}/banks/{bank_id}", data={"status": "approved", "rate": "2,5",
                                                              "fee": "15 000", "note": "без залога"})
        page = c.get(f"/crm/deals/{deal_id}").text
        assert "Сбербанк — Одобрена" in page and "«В банках» → «Одобрена»" in page
        assert 'value="15000"' in page and "без залога" in page

        c.post(f"/crm/deals/{deal_id}", data={"stage": "issued", "product": "БГ на исполнение контракта",
                                              "amount": "625000", "commission": "12 000"})
        c.post(f"/crm/deals/{deal_id}/notes", data={"text": "Клиент подписал", "kind": "call"})
        page = c.get(f"/crm/deals/{deal_id}").text
        assert "«Одобрена» → «Выдана»" in page and "Клиент подписал" in page and 'value="12000"' in page
        assert f"/crm/deals/{deal_id}" not in c.get("/crm/deals").text          # закрытые не в работе
        assert f"/crm/deals/{deal_id}" in c.get("/crm/deals?stage=issued").text
        assert c.get("/crm/deals/999").status_code == 404

        c.post("/logout")
        login(c, "a@example.com")
        assert "Выдана" in c.get("/tenders").text


def test_client_card(tmp_path):
    with make_client(tmp_path, [FakeFns()], admin_emails=(ADMIN,)) as c:
        c.post("/register", data={"email": ADMIN, "password": "password1"})
        assert c.post("/crm/clients", data={"inn": "123"}).status_code == 200
        assert "10 или 12 цифр" in c.post("/crm/clients", data={"inn": "123"}).text

        r = c.post("/crm/clients", data={"inn": INN, "source": "Рекомендация", "run_check": "1"})
        client_url = r.url.path
        assert client_url.startswith("/crm/clients/")
        assert c.post("/crm/clients", data={"inn": INN}).url.path == client_url   # тот же ИНН — та же карточка

        check_id = int(re.search(r'href="/checks/(\d+)"', c.get(client_url).text).group(1))
        wait_status(c, check_id, {"done"})
        page = c.get(client_url).text
        assert "ПАО СБЕРБАНК" in page and "1027700132195" in page and "Запущена проверка" in page

        c.post(f"{client_url}/contacts", data={"name": "Иван Петров", "position": "Финдиректор",
                                               "phone": "+7 900 000-00-00"})
        c.post(f"{client_url}/notes", data={"text": "Нужна БГ на 5 млн", "kind": "call"})
        c.post("/crm/deals", data={"client_id": client_url.rsplit("/", 1)[1],
                                   "product": "БГ на возврат аванса", "amount": "5 000 000"})
        page = c.get(client_url).text
        assert "Иван Петров" in page and "Финдиректор" in page and "Нужна БГ на 5 млн" in page
        assert "БГ на возврат аванса" in page and "5 000 000 ₽" in page

        contact_id = re.search(rf'{client_url}/contacts/(\d+)/delete', page).group(1)
        c.post(f"{client_url}/contacts/{contact_id}/delete")
        assert "Иван Петров" not in c.get(client_url).text

        listing = c.get("/crm/clients?q=сбер").text
        assert "ПАО СБЕРБАНК" in listing and "в работе: 1" in listing
        assert "Клиентов пока нет" in c.get("/crm/clients?q=нет-такого").text

        # Сотрудник видит проверку, запущенную не им, клиент — только свои.
        assert c.get(f"/checks/{check_id}").status_code == 200
        c.post("/logout")
        register(c)
        assert c.get(f"/checks/{check_id}").status_code == 404


def test_old_database_gets_role_column(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / "t.db")
    db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR(255), password_hash VARCHAR(255),"
               " telegram_id BIGINT, created_at DATETIME)")
    db.commit()
    db.close()
    with make_client(tmp_path, [FakeFns()], admin_emails=(ADMIN,)) as c:
        assert c.post("/register", data={"email": ADMIN, "password": "password1"}).url.path == "/crm/deals"
