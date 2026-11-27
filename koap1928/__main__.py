"""Проверка из командной строки.

    python -m koap1928 7707083893            по ИНН или ОГРН (файл скачивается из ЕИС)
    python -m koap1928 7707083893 --file reestr.xlsx   по файлу, скачанному вручную
    python -m koap1928 --dump                 показать, как разобран файл (первые записи)
"""

import argparse
import asyncio
import json
import logging
import re
import sys

from .check import check_koap1928
from .client import Koap1928Client


async def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m koap1928")
    ap.add_argument("ident", nargs="?", help="ИНН (10 или 12 цифр) или ОГРН")
    ap.add_argument("--file", help="локальный .xlsx вместо скачивания из ЕИС")
    ap.add_argument("--dump", action="store_true", help="вывести первые 5 записей и число записей")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    async with Koap1928Client(file_path=args.file) as client:
        reg = await client.registry()
    if args.dump:
        print(f"Записей: {len(reg)}, источник: {reg.source}")
        for r in reg.records[:5]:
            print(json.dumps(r.to_dict(), ensure_ascii=False))
        return 0
    if not args.ident:
        ap.error("нужен ИНН/ОГРН или --dump")
    ident = re.sub(r"\D", "", args.ident)
    is_ogrn = len(ident) in (13, 15)
    report = check_koap1928(reg, inn=ident if not is_ogrn else "", ogrn=ident if is_ogrn else None)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        for label, value in report.summary():
            print(f"{label}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
