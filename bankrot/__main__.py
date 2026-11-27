"""Проверка из командной строки.

    python -m bankrot 7707083893                 компания или физлицо по ИНН
    python -m bankrot 7707083893 --card card.json  плюс связанные лица из карточки ФНС
                                                  (её сохраняет python -m pbnalog ... -o card.json)
    python -m bankrot --name "Иванов Иван Иванович"  поиск физлиц-банкротов по ФИО
"""

import argparse
import asyncio
import json
import logging
import re
import sys
from pathlib import Path

from .check import check_bankruptcy
from .client import FedresursClient
from .parties import Party, extract_parties


async def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m bankrot")
    ap.add_argument("inn", nargs="?", help="ИНН компании (10 цифр) или физлица/ИП (12 цифр)")
    ap.add_argument("--card", help="JSON из python -m pbnalog -o: проверить и связанных лиц")
    ap.add_argument("--name", help="поиск физлиц-банкротов по ФИО (совпадения надо проверять)")
    ap.add_argument("--json", action="store_true", help="вывести полный отчёт в JSON")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)

    async with FedresursClient() as client:
        if args.name:
            for b in await client.search_person_bankrupts(args.name):
                print(f"{b.name}  ИНН {b.inn or '-'}  {b.describe()}  {b.url or ''}")
            return 0
        if not args.inn:
            ap.error("нужен ИНН или --name")
        inn = re.sub(r"\D", "", args.inn)
        related = []
        if args.card:
            card = json.loads(Path(args.card).read_text(encoding="utf-8"))
            related = extract_parties(card, exclude={inn})
        report = await check_bankruptcy(client, Party(inn=inn, name=inn), related)

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        for label, value in report.summary():
            print(f"{label}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
