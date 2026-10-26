"""Проверка из командной строки: python -m pbnalog 7707083893 [-o card.json]

Если сайт попросит капчу, картинка сохранится в файл, а ответ нужно ввести вручную.
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from .client import PbNalogClient


async def manual_solver(image: bytes) -> str:
    path = Path("captcha.png").resolve()
    path.write_bytes(image)
    return await asyncio.to_thread(input, f"Капча сохранена в {path}. Введите цифры: ")


async def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m pbnalog")
    ap.add_argument("query", help="ИНН или ОГРН/ОГРНИП")
    ap.add_argument("-o", "--output", help="сохранить полный ответ в JSON-файл")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    async with PbNalogClient(solver=manual_solver) as pb:
        result = await pb.lookup(args.query)

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"Сохранено в {args.output}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
