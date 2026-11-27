"""Проверка из командной строки (запускать на сервере в РФ).

    python -m accblock 7707083893             решения ФНС (капча — в терминале) и ЦБ
    python -m accblock 7707083893 --dump      показать сырой ответ ФНС и ЦБ
    python -m accblock 7707083893 --bik 044525225   другой БИК в запросе к ФНС
    python -m accblock --zsk-page             скачать форму ЦБ и найти адрес запроса
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import tempfile

from .check import AccountBlockReport, FnsResult, ZskResult
from .fns import FnsBlocksClient
from .zsk import ZskClient, find_endpoints


async def ask_in_terminal(image: bytes) -> str:
    fd, path = tempfile.mkstemp(suffix=".gif", prefix="fns-captcha-")
    with os.fdopen(fd, "wb") as f:
        f.write(image)
    return input(f"Капча ФНС сохранена в {path}. Цифры с картинки: ")


async def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m accblock")
    ap.add_argument("inn", nargs="?")
    ap.add_argument("--bik", help="БИК для запроса к ФНС (по умолчанию FNS_BI_BIK или Сбербанк)")
    ap.add_argument("--dump", action="store_true", help="вывести сырые ответы")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--zsk-page", action="store_true", help="форма ЦБ: кандидаты в адрес запроса")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    if args.zsk_page:
        zsk = ZskClient()
        try:
            html = await zsk.page()
        finally:
            await zsk.aclose()
        with open("zsk-page.html", "w", encoding="utf-8") as f:
            f.write(html)
        print("Страница сохранена в zsk-page.html. Кандидаты в адрес запроса:")
        for e in find_endpoints(html):
            print("  ", e)
        return 0
    if not args.inn:
        ap.error("нужен ИНН или --zsk-page")
    inn = re.sub(r"\D", "", args.inn)

    async with FnsBlocksClient(bik=args.bik) as fns_client:
        try:
            decisions = await fns_client.decisions(inn, solver=ask_in_terminal)
            fns = FnsResult(True, decisions, bik=fns_client.bik)
        except Exception as e:
            fns = FnsResult(False, error=f"{type(e).__name__}: {e}", bik=fns_client.bik)
        if args.dump:
            print("ФНС, сырой ответ:", json.dumps(fns_client.last_raw, ensure_ascii=False, indent=2))
    zsk_client = ZskClient()
    try:
        high, text = await zsk_client.check(inn)
        zsk = ZskResult(True, high, text)
    except Exception as e:
        zsk = ZskResult(False, error=str(e))
    finally:
        await zsk_client.aclose()
    if args.dump and zsk_client.last_raw is not None:
        print("ЦБ, сырой ответ:", str(zsk_client.last_raw)[:3000])

    report = AccountBlockReport(inn, fns, zsk)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.verdict)
        for label, value in report.summary():
            print(f"{label}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
