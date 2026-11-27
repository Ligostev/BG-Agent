"""Согласия, уведомления клиентам и рекламные рассылки."""

import re
from datetime import date

from app.outreach import Mailer, deliver
from app.tenders_sync import sync

from test_app import FakeFns, make_client
from test_crm import ADMIN, login
from test_tenders_app import FakeEis


class FakeMailer(Mailer):
    def __init__(self):
        self.sent = []

    @property
    def ready(self):
        return True

    async def send(self, to, subject, body, unsubscribe=None):
        self.sent.append((to, subject, body, unsubscribe))


def test_register_requires_pd_consent(tmp_path):
    with make_client(tmp_path, [FakeFns()]) as c:
        r = c.post("/register", data={"email": "a@example.com", "password": "password1"})
        assert r.status_code == 400 and "согласия на обработку" in r.text
        assert "обработку моих персональных данных" in c.get("/legal/pd").text
        c.post("/register", data={"email": "a@example.com", "password": "password1", "pd": "1", "ads": "1"},
               headers={"user-agent": "pytest"})
        page = c.get("/settings").text
        assert "включено с" in page and "a@example.com" in page


def test_notifications_consents_and_campaign(tmp_path):
    sent_tg = []

    async def telegram(chat, text):
        sent_tg.append((chat, text))

    mailer = FakeMailer()
    with make_client(tmp_path, [FakeFns()], admin_emails=(ADMIN,), public_url="https://lk.example") as c:
        sm = c.app.state.sessionmaker
        # Клиент без согласия на рекламу оставляет заявку.
        c.post("/register", data={"email": "client@example.com", "password": "password1", "pd": "1"})
        c.post("/tenders/profile", data={"okpd2": "43.91", "regions": "77", "min_days_left": "0"})
        c.portal.call(sync, sm, FakeEis(), date(2026, 11, 18))
        c.post("/tenders/0373200000126000123/guarantee")
        assert "выключено" in c.get("/settings").text
        # Второй клиент согласен на рекламу.
        c.post("/logout")
        c.post("/register", data={"email": "fan@example.com", "password": "password1", "pd": "1", "ads": "1"})

        c.post("/logout")
        c.post("/register", data={"email": ADMIN, "password": "password1", "pd": "1"})
        deal_id = int(re.search(r'/crm/deals/(\d+)">№\d+ · БГ на исполнение', c.get("/crm/deals").text).group(1))
        c.post(f"/crm/deals/{deal_id}/banks", data={"bank": "ВТБ"})      # → «В банках», клиенту уведомление
        page = c.get(f"/crm/deals/{deal_id}").text
        assert "клиенту отправлено уведомление «В банках»" in page

        # Согласие, полученное менеджером вне кабинета, без источника не принимается.
        client_url = c.post("/crm/clients", data={"title": "ООО Оффлайн"}).url.path
        assert c.post(f"{client_url}/consents", data={"channel": "email", "address": "x@ex.ru",
                                                      "source": ""}).status_code in (400, 422)
        c.post(f"{client_url}/consents", data={"channel": "email", "address": "Offline@Ex.ru",
                                               "source": "анкета от 01.12.2026"})
        assert "offline@ex.ru" in c.get(client_url).text

        # Рассылка: только тем, кто согласен.
        r = c.post("/crm/campaigns", data={"title": "Акция", "channel": "email", "subject": "Скидка на БГ",
                                           "body": "Гарантии со скидкой до конца года", "group": "all"})
        camp = r.url.path
        page = c.get(camp).text
        assert "Получателей с действующим согласием: <b>2</b>" in page
        assert "client@example.com" not in page and "fan@example.com" in page and "offline@ex.ru" in page
        assert "Реклама." in page
        c.post(f"{camp}/send")                                   # без подтверждения ничего не уходит
        assert "Черновик" in c.get("/crm/campaigns").text
        c.post(f"{camp}/send", data={"confirm": "1"})

        stats = c.portal.call(lambda: deliver(sm, telegram=telegram, mailer=mailer, public_url="https://lk.example"))
        assert stats == {"sent": 3, "failed": 0, "skipped": 0}
        service = [m for m in mailer.sent if m[0] == "client@example.com"]
        assert service and "заявка отправлена в банки" in service[0][2] and service[0][3] is None
        ads = {m[0]: m for m in mailer.sent if m[0] != "client@example.com"}
        assert set(ads) == {"fan@example.com", "offline@ex.ru"}
        body, unsub = ads["fan@example.com"][2], ads["fan@example.com"][3]
        assert "Гарантии со скидкой" in body and "Реклама." in body and unsub in body
        token = unsub.rsplit("/", 1)[1]
        assert "Отписаться от рассылки?" in c.get(f"/u/{token}").text
        assert "Вы отписаны" in c.post(f"/u/{token}").text
        assert c.get("/u/нет-такого").status_code == 404
        assert "Отправлена" in c.get("/crm/campaigns").text

        # Вторая рассылка уже не найдёт отписавшегося.
        r = c.post("/crm/campaigns", data={"title": "Ещё", "channel": "email", "body": "Текст", "group": "all"})
        page = c.get(r.url.path).text
        assert "<b>1</b>" in page and "fan@example.com" not in page
        # Отзыв после постановки в очередь: письмо не уходит.
        c.post(f"{r.url.path}/send", data={"confirm": "1"})
        consent_id = re.search(rf'{client_url}/consents/(\d+)/revoke', c.get(client_url).text).group(1)
        c.post(f"{client_url}/consents/{consent_id}/revoke")
        assert "отозвано" in c.get(client_url).text
        stats = c.portal.call(lambda: deliver(sm, telegram=telegram, mailer=mailer, public_url="https://lk.example"))
        assert stats == {"sent": 0, "failed": 0, "skipped": 1}

        # Отписка через /stop в боте и отказ от уведомлений.
        hdr = {"X-Bot-Token": "bot-secret", "X-Telegram-Id": "777"}
        assert c.post("/api/telegram/stop", headers=hdr).json() == {"ok": True, "revoked": 0}
        assert c.post("/api/telegram/stop", headers={"X-Telegram-Id": "777"}).status_code == 401

        login(c, "fan@example.com")
        assert c.get("/crm/campaigns").status_code == 403
        assert "выключено" in c.get("/settings").text
        c.post("/settings/ads/email", data={"on": "1"})
        assert "включено с" in c.get("/settings").text
        c.post("/settings/ads/email")
        assert "выключено" in c.get("/settings").text
        c.post("/settings/notify")
        assert "Включить уведомления" in c.get("/settings").text
