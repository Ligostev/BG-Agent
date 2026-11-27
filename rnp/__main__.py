"""Проверка из командной строки: python -m rnp 7707083893"""

import argparse
import asyncio
import json
import logging
import sys

from .client import RnpClient


async def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m rnp")
    ap.add_argument("inn", help="ИНН поставщика")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    async with RnpClient() as rnp:
        entries = await rnp.check(args.inn)
    print(json.dumps([e.to_dict() for e in entries], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
