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


def test_tasks_and_reminders(tmp_path):
    from datetime import datetime, timedelta, timezone

    from app.crm_tasks import parse_due, send_reminders

    sent = []

    async def send(chat, text):
        sent.append((chat, text))

    with make_client(tmp_path, [FakeFns()], admin_emails=(ADMIN,)) as c:
        sm = c.app.state.sessionmaker
        c.post("/register", data={"email": ADMIN, "password": "password1"})
        client_url = c.post("/crm/clients", data={"inn": INN, "title": "ООО Ромашка"}).url.path
        cid = client_url.rsplit("/", 1)[1]

        # Привязка Telegram: код из кабинета, бот присылает его в API.
        c.post("/crm/telegram/link")
        page = c.get("/crm/tasks").text
        code = re.search(r"/start link_([\w-]+)", page).group(1)
        hdr = {"X-Bot-Token": "bot-secret", "X-Telegram-Id": "555"}
        assert c.post("/api/telegram/link", json={"code": "нет"}, headers=hdr).status_code == 404
        assert c.post("/api/telegram/link", json={"code": code}, headers={"X-Telegram-Id": "555"}).status_code == 401
        assert c.post("/api/telegram/link", json={"code": code}, headers=hdr).json()["staff"] is True
        assert "Telegram подключён" in c.get("/crm/tasks").text

        c.post(f"{client_url}", data={"title": "ООО Ромашка", "inn": INN, "manager_id": "1"})
        assert "Ответственный: boss@example.com" in c.get(client_url).text
        c.post("/crm/tasks", data={"title": "Перезвонить по балансу", "due": "2020-01-01T10:00",
                                   "client_id": cid, "remind": "1", "back": client_url})
        c.post("/crm/tasks", data={"title": "Без срока", "client_id": cid, "back": "https://evil.example"})
        page = c.get(client_url).text
        assert "Перезвонить по балансу" in page and "до 01.01.2020 10:00" in page
        tasks = c.get("/crm/tasks").text
        assert re.search(r"Просрочено.*?· 1.*Перезвонить по балансу", tasks, re.S)

        # Выданная гарантия, которая скоро кончается, порождает задачу; напоминание уходит в Telegram.
        c.post("/crm/deals", data={"client_id": cid, "product": "БГ на исполнение контракта"})
        soon = (datetime.now(timezone.utc) + timedelta(days=10)).date().isoformat()
        c.post("/crm/deals/1", data={"stage": "issued", "product": "БГ на исполнение контракта",
                                     "expires_at": soon})
        stats = c.portal.call(send_reminders, sm, send, "https://lk.example")
        assert stats == {"created": 1, "sent": 2}
        texts = [t for _, t in sent]
        assert all(chat == 555 for chat, _ in sent)
        assert any("Перезвонить по балансу" in t and "ООО Ромашка" in t and "https://lk.example/crm/tasks" in t
                   for t in texts)
        assert any("Гарантия по сделке №1 заканчивается" in t for t in texts)
        # Повторный запуск ничего не шлёт и не дублирует задачу.
        assert c.portal.call(send_reminders, sm, send, "") == {"created": 0, "sent": 0}

        # Готово и «+1 день».
        task_id = re.search(r'/crm/tasks/(\d+)/done', c.get(client_url).text).group(1)
        c.post(f"/crm/tasks/{task_id}/done", data={"back": client_url})
        assert "Выполнено:" in c.get(client_url).text
        assert "Недавно выполненные" in c.get("/crm/tasks").text

    assert parse_due("2026-11-28T10:00") == datetime(2026, 11, 28, 7, 0, tzinfo=timezone.utc)
    assert parse_due("2026-11-28").hour == 7 and parse_due("мусор") is None


def test_manager_sees_own_and_unassigned(tmp_path):
    with make_client(tmp_path, [FakeFns()], admin_emails=(ADMIN,)) as c:
        for email in ("m1@example.com", "m2@example.com"):
            register(c, email)
            c.post("/logout")
        c.post("/register", data={"email": ADMIN, "password": "password1"})
        assert "Пусть сначала" not in c.post("/crm/staff", data={"email": "M1@example.com", "role": "manager"}).text
        c.post("/crm/staff", data={"email": "m2@example.com", "role": "manager"})
        assert "не найден" in c.post("/crm/staff", data={"email": "x@example.com", "role": "manager"}).text
        assert "ADMIN_EMAILS" in c.post("/crm/staff", data={"email": ADMIN, "role": "client"}).text
        staff = c.get("/crm/staff").text
        assert "m1@example.com" in staff and "m2@example.com" in staff
        pool = c.post("/crm/clients", data={"title": "Общий клиент"}).url.path   # у админа без ответственного

        login(c, "m1@example.com")
        assert c.get("/crm/staff").status_code == 403
        own = c.post("/crm/clients", data={"inn": INN, "title": "Клиент первого"}).url.path
        c.post("/crm/deals", data={"client_id": own.rsplit("/", 1)[1], "product": "БГ на исполнение контракта"})
        c.post("/crm/tasks", data={"title": "Задача первого", "client_id": own.rsplit("/", 1)[1], "remind": "1"})
        assert c.post(own, data={"title": "Клиент первого", "inn": INN, "manager_id": "3"}).status_code == 403
        assert c.get(pool).status_code == 200

        login(c, "m2@example.com")
        listing = c.get("/crm/clients").text
        assert "Общий клиент" in listing and "Клиент первого" not in listing
        assert c.get(own).status_code == 404
        assert c.get("/crm/deals/1").status_code == 404 and "Клиент первого" not in c.get("/crm/deals").text
        assert "Задача первого" not in c.get("/crm/tasks").text
        assert c.post("/crm/tasks/1/done").status_code == 404
        assert "закреплён за другим менеджером" in c.post("/crm/clients", data={"inn": INN}).text

        login(c, ADMIN)
        assert "Клиент первого" in c.get("/crm/clients").text
        assert "Задача первого" in c.get("/crm/tasks?who=all").text
        # Снятие роли возвращает клиентов в общий список.
        c.post("/crm/staff", data={"email": "m1@example.com", "role": "client"})
        login(c, "m2@example.com")
        assert "Клиент первого" in c.get("/crm/clients").text
        login(c, "m1@example.com")
        assert c.get("/crm/deals").status_code == 403


def test_reports_and_excel(tmp_path):
    import io

    from openpyxl import Workbook, load_workbook

    with make_client(tmp_path, [FakeFns()], admin_emails=(ADMIN,)) as c:
        c.post("/register", data={"email": ADMIN, "password": "password1"})
        a = c.post("/crm/clients", data={"inn": INN, "title": "ООО Альфа", "source": "Реклама"}).url.path
        b = c.post("/crm/clients", data={"title": "ООО Бета", "source": "Рекомендация"}).url.path
        for url, product, amount in ((a, "БГ на исполнение контракта", "1000000"),
                                     (a, "БГ на участие (обеспечение заявки)", "50000"),
                                     (b, "БГ на исполнение контракта", "2000000")):
            c.post("/crm/deals", data={"client_id": url.rsplit("/", 1)[1], "product": product, "amount": amount})
        c.post("/crm/deals/1/banks", data={"bank": "ВТБ"})
        c.post("/crm/deals/1/banks/1", data={"status": "issued", "fee": "20000"})
        c.post("/crm/deals/1", data={"stage": "issued", "product": "БГ на исполнение контракта",
                                     "amount": "1000000", "commission": "8000"})
        c.post("/crm/deals/3", data={"stage": "lost", "product": "БГ на исполнение контракта"})

        page = c.get("/crm/reports").text
        assert "Новых сделок</th><td>3" in page
        assert "<b>1</b> на 1 000 000 ₽" in page and "<b>8 000 ₽</b>" in page
        assert "Доля выдач среди закрытых</th><td>50%" in page
        assert "Реклама" in page and "Рекомендация" in page and "ВТБ" in page and "20 000 ₽" in page
        assert c.get("/crm/reports?period=year").status_code == 200

        wb = load_workbook(io.BytesIO(c.get("/crm/deals.xlsx").content))
        rows = list(wb.active.iter_rows(values_only=True))
        assert rows[0][:5] == ("№", "Клиент", "ИНН", "Продукт", "Этап") and len(rows) == 4
        assert rows[1][1] == "ООО Альфа" and rows[1][4] == "Выдана" and rows[1][6] == 8000
        wb = load_workbook(io.BytesIO(c.get("/crm/clients.xlsx").content))
        assert [r[0] for r in wb.active.iter_rows(min_row=2, values_only=True)] == ["ООО Альфа", "ООО Бета"]

        # Загрузка списка клиентов: заголовок не в первой строке, ИНН без ведущего нуля, дубль, мусор.
        wb = Workbook()
        ws = wb.active
        ws.append(["Список клиентов"])
        ws.append(["ИНН", "Наименование компании", "Контактное лицо", "Телефон", "Откуда"])
        ws.append([INN, "дубль", "", "", ""])
        ws.append([772345678, "ООО Гамма", "Анна", "+7 999", "Выставка"])   # 9 цифр: ноль съеден Excel
        ws.append(["123", "Плохой ИНН", "", "", ""])
        ws.append(["", "ИП Дельта", "", "", ""])
        buf = io.BytesIO()
        wb.save(buf)
        r = c.post("/crm/import/clients", files={"file": ("list.xlsx", buf.getvalue(), "application/octet-stream")})
        assert "Добавлено клиентов: 2, уже были в CRM: 1, с неверным ИНН: 1" in r.text
        listing = c.get("/crm/clients?q=гамма").text
        assert "ООО Гамма" in listing and "0772345678" in listing and "Выставка" in listing
        bad = c.post("/crm/import/clients", files={"file": ("x.csv", "a;b\n1;2\n".encode(), "text/csv")})
        assert "Не получилось разобрать" in bad.text
