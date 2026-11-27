"""Проверка ключа и карточки: python -m dadata 7707083893 [--json]"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .client import DaDataClient, DaDataError
from .party import parse_party


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m dadata", description=__doc__)
    ap.add_argument("query", help="ИНН или ОГРН")
    ap.add_argument("--json", action="store_true", help="сырой ответ DaData")
    args = ap.parse_args(argv)
    client = DaDataClient()
    try:
        suggestion = await client.find_party(args.query)
    except DaDataError as e:
        print(f"Ошибка DaData: {e}", file=sys.stderr)
        return 2
    finally:
        await client.aclose()
    if suggestion is None:
        print("Не найдено")
        return 1
    if args.json:
        print(json.dumps(suggestion, ensure_ascii=False, indent=2))
        return 0
    card = parse_party(suggestion)
    for label, value in card.summary:
        print(f"{label}: {value}")
    for p in card.parties:
        print(f"  {p['role']}: {p['name']}" + (f" (ИНН {p['inn']})" if p["inn"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
