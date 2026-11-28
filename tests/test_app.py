"""Кабинет и API на SQLite с подставными источниками вместо сайтов ФНС."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.sources import SourceContext, SourceData

INN = "7707083893"


class FakeFns:
    name, title = "fns", "ФНС"

    def __init__(self, captcha: bool = False):
        self.captcha = captcha
        self.answers = []

    async def fetch(self, ctx: SourceContext) -> SourceData:
        if self.captcha:
            self.answers.append(await ctx.ask_captcha(b"PNG"))
        return SourceData(found=True, summary=[("Наименование", "ПАО СБЕРБАНК")],
                          data={"inn": ctx.query},
                          identity={"title": "ПАО СБЕРБАНК", "inn": INN, "ogrn": "1027700132195"})


class Broken:
    name, title = "rnp", "РНП"

    async def fetch(self, ctx: SourceContext) -> SourceData:
        assert ctx.previous["fns"].found
        raise RuntimeError("сайт недоступен")


def make_client(tmp_path, sources, **kw):
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/t.db", redis_url="",
                        bot_api_token="bot-secret", captcha_timeout=5, **kw)
    return TestClient(create_app(settings, sources=sources))


def wait_status(client, check_id, statuses, headers=None):
    for _ in range(100):
        data = client.get(f"/api/checks/{check_id}", headers=headers).json()
        if data["status"] in statuses:
            return data
        client.portal.call(asyncio.sleep, 0.05)
    raise AssertionError(f"статус так и не стал {statuses}: {data}")


def register(client, email="a@example.com"):
    r = client.post("/register", data={"email": email, "password": "password1", "pd": "1"})
    assert r.status_code == 200 and r.url.path == "/checks"


def test_login_required(tmp_path):
    with make_client(tmp_path, [FakeFns()]) as c:
        r = c.get("/checks")
        assert r.url.path == "/login"
        assert c.post("/api/checks", json={"query": INN}).status_code == 401


def test_register_login_and_check(tmp_path):
    with make_client(tmp_path, [FakeFns(), Broken()]) as c:
        register(c)
        c.post("/logout")
        assert c.post("/login", data={"email": "a@example.com", "password": "wrong"}).status_code == 400
        assert c.post("/login", data={"email": "A@example.com", "password": "password1"}).url.path == "/checks"

        assert c.post("/checks", data={"query": "123"}).status_code == 400
        r = c.post("/checks", data={"query": f" {INN} "})
        check_id = int(r.url.path.rsplit("/", 1)[1])
        data = wait_status(c, check_id, {"done", "error"})
        assert data["status"] == "done" and data["title"] == "ПАО СБЕРБАНК"
        assert [(x["source"], x["status"]) for x in data["results"]] == [("fns", "found"), ("rnp", "error")]
        assert "сайт недоступен" in data["results"][1]["error"]

        page = c.get(f"/checks/{check_id}").text
        assert "ПАО СБЕРБАНК" in page and "сайт недоступен" in page
        assert "ПАО СБЕРБАНК" in c.get("/checks").text
        assert c.get(f"/api/checks/{check_id}/data/fns").json() == {"inn": INN}


def test_checks_are_private(tmp_path):
    with make_client(tmp_path, [FakeFns()]) as c:
        register(c)
        check_id = c.post("/api/checks", json={"query": INN}).json()["id"]
        c.post("/logout")
        register(c, "b@example.com")
        assert c.get(f"/api/checks/{check_id}").status_code == 404
        assert c.get(f"/checks/{check_id}").status_code == 404


def test_bot_api_with_captcha(tmp_path):
    fns = FakeFns(captcha=True)
    bot = {"X-Bot-Token": "bot-secret", "X-Telegram-Id": "42"}
    with make_client(tmp_path, [fns]) as c:
        assert c.post("/api/checks", json={"query": INN},
                      headers={**bot, "X-Bot-Token": "wrong"}).status_code == 401
        check_id = c.post("/api/checks", json={"query": INN}, headers=bot).json()["id"]
        wait_status(c, check_id, {"captcha"}, bot)
        assert c.get(f"/api/checks/{check_id}/captcha", headers=bot).content == b"PNG"
        assert c.post(f"/api/checks/{check_id}/captcha", json={"answer": " 123456 "}, headers=bot).json() == {"ok": True}
        assert wait_status(c, check_id, {"done"}, bot)["title"] == "ПАО СБЕРБАНК"
        assert fns.answers == ["123456"]
        assert c.post(f"/api/checks/{check_id}/captcha", json={"answer": "1"}, headers=bot).status_code == 409
        # тот же telegram_id видит свою историю, другой нет
        assert [x["id"] for x in c.get("/api/checks", headers=bot).json()] == [check_id]
        assert c.get("/api/checks", headers={**bot, "X-Telegram-Id": "43"}).json() == []


def test_static_brand_assets(tmp_path):
    with make_client(tmp_path, [FakeFns()]) as c:
        page = c.get("/login").text
        assert "/static/css/tenfin.css?v=" in page and "/static/brand/logo.svg?v=" in page
        for path in ("/static/css/tenfin.css", "/static/brand/logo.svg", "/static/brand/favicon.svg",
                     "/static/fonts/manrope-cyrillic.woff2", "/static/fonts/unbounded-cyrillic.woff2"):
            assert c.get(path).status_code == 200, path
