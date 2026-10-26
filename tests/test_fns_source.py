"""Источник ФНС поверх pbnalog: капча уходит в ask_captcha текущей проверки."""

import asyncio

from app.sources import SourceContext
from app.sources.fns import FnsSource
from tests.test_client import FakeSite, make_client


def test_fns_source_identity_and_captcha():
    site = FakeSite(captcha_on_search=True)
    asked = []

    async def ask(image: bytes) -> str:
        asked.append(image)
        return "123456"

    async def go():
        source = FnsSource(make_client(site))
        try:
            return await source.fetch(SourceContext(query="1027700132195", ask_captcha=ask))
        finally:
            await source.aclose()

    data = asyncio.run(go())
    assert data.found and asked == [b"\x89PNG-fake"]
    assert data.identity == {"kind": "ul", "title": "ПАО СБЕРБАНК",
                             "inn": "7707083893", "ogrn": "1027700132195"}
    assert ("Тип", "Юридическое лицо") in data.summary


def test_fns_source_not_found():
    async def go():
        source = FnsSource(make_client(FakeSite(rows=[])))
        try:
            return await source.fetch(SourceContext(query="7707083893", ask_captcha=None))
        finally:
            await source.aclose()

    assert asyncio.run(go()).found is False
