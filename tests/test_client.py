"""Тесты клиента на имитации сайта (формат ответов как у веб-интерфейса pb.nalog.ru)."""

import asyncio
import json
from urllib.parse import parse_qs

import httpx
import pytest

from pbnalog.client import BASE_URL, CaptchaRequired, NotFound, PbNalogClient


class FakeSite:
    def __init__(self, *, captcha_on_search=False, pending_responses=1, rows=None):
        self.captcha_on_search = captcha_on_search
        self.pending = pending_responses
        self.rows = rows if rows is not None else [
            {"token": "TOK-OTHER", "inn": "7707083893", "ogrn": "1000000000000", "namec": "Филиал"},
            {"token": "TOK-SBER", "inn": "7707083893", "ogrn": "1027700132195", "namec": "ПАО СБЕРБАНК"},
        ]
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        form = {k: v[0] for k, v in parse_qs(request.content.decode(), keep_blank_values=True).items()}
        self.calls.append((path, form))
        if path == "/search-proc.json":
            if self.captcha_on_search and form.get("pbCaptchaToken") != "PB-OK":
                return httpx.Response(400, json={"ERRORS": {"pbCaptcha": ["Требуется ввести цифры"]}})
            assert form["mode"] == "search-ul" and form["queryUl"]
            return httpx.Response(200, json={"ul": {"data": self.rows, "rowCount": len(self.rows)}})
        if path == "/captcha-dialog.html":
            return httpx.Response(200, text='<div><img src="/static/captcha.bin?r=1&amp;a=CAP-TOKEN&amp;version=2"></div>')
        if path == "/static/captcha.bin":
            assert request.url.params["a"] == "CAP-TOKEN"
            return httpx.Response(200, content=b"\x89PNG-fake")
        if path == "/captcha-proc.json":
            if form == {"captcha": "123456", "captchaToken": "CAP-TOKEN"}:
                return httpx.Response(200, text='"PB-OK"')
            return httpx.Response(400, json={"ERRORS": {"captcha": ["Неверные цифры"]}})
        if path == "/company-proc.json":
            if form["method"] == "get-request":
                return httpx.Response(200, json={"id": 42, "token": form["token"] + "-2"})
            assert form["id"] == "42" and form["method"] == "get-response"
            assert form["token"].endswith("-2") and "pbCaptchaToken" not in form
            if self.pending > 0:
                self.pending -= 1
                return httpx.Response(200, text="")
            return httpx.Response(200, json={"vyp": {"ИНН": "7707083893"}, "token": "x"})
        return httpx.Response(404)


def make_client(site, **kw):
    http = httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(site))
    return PbNalogClient(http=http, min_interval=0, poll_delay=0, **kw)


def run(coro):
    return asyncio.run(coro)


def test_lookup_picks_exact_match_and_polls():
    site = FakeSite(pending_responses=2)

    async def go():
        async with make_client(site) as pb:
            return await pb.lookup("1027700132195")

    res = run(go())
    assert res["kind"] == "ul"
    assert res["hit"]["token"] == "TOK-SBER"
    assert res["card"]["vyp"]["ИНН"] == "7707083893"


def test_captcha_is_solved_and_token_reused():
    site = FakeSite(captcha_on_search=True)
    answers = iter(["000000", "123456"])

    async def solver(image: bytes) -> str:
        assert image.startswith(b"\x89PNG")
        return next(answers)

    async def go():
        async with make_client(site, solver=solver) as pb:
            await pb.lookup("7707083893")
            await pb.search("7707083893", kind="ul")

    run(go())
    searches = [f for p, f in site.calls if p == "/search-proc.json"]
    assert [f["pbCaptchaToken"] for f in searches] == ["", "PB-OK", "PB-OK"]


def test_captcha_without_solver_raises():
    async def go():
        async with make_client(FakeSite(captcha_on_search=True)) as pb:
            await pb.lookup("7707083893")

    with pytest.raises(CaptchaRequired):
        run(go())


def test_not_found_and_invalid_input():
    async def go(q):
        async with make_client(FakeSite(rows=[])) as pb:
            await pb.lookup(q)

    with pytest.raises(NotFound):
        run(go("7707083893"))
    with pytest.raises(ValueError):
        run(go("7707083894"))
