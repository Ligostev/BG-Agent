"""Проверка из командной строки:

    python -m fssp --legal "ООО РОМАШКА"
    python -m fssp --person Иванов Иван Иванович --birth 01.01.1980

Если сайт попросит капчу, картинка сохранится в файл, а ответ нужно ввести вручную.
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from .client import ALL_REGIONS, FsspClient


async def manual_solver(image: bytes) -> str:
    path = Path("captcha.png").resolve()
    path.write_bytes(image)
    return await asyncio.to_thread(input, f"Капча сохранена в {path}. Введите символы: ")


async def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m fssp")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--legal", metavar="НАИМЕНОВАНИЕ")
    g.add_argument("--person", nargs="+", metavar="ФИО")
    g.add_argument("--number", metavar="НОМЕР_ИП")
    ap.add_argument("--birth", default="", help="дата рождения ДД.ММ.ГГГГ (для --person)")
    ap.add_argument("--region", type=int, default=ALL_REGIONS, help="код региона, по умолчанию все")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    async with FsspClient(solver=manual_solver) as fssp:
        if args.legal:
            rows = await fssp.search_legal(args.legal, region=args.region)
        elif args.number:
            rows = await fssp.search_number(args.number)
        else:
            last, first, *rest = args.person + [""]
            rows = await fssp.search_person(last, first, " ".join(rest).strip(),
                                            args.birth, region=args.region)
    print(json.dumps([r.to_dict() for r in rows], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
