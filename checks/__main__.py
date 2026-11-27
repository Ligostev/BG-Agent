"""Полная проверка из командной строки: python -m checks 7707083893 [-o report.json]

Капчи (ФНС — цифры, ФССП — буквы и цифры) сохраняются в captcha.png и вводятся вручную.
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from fssp import FsspClient
from fssp.__main__ import manual_solver as fssp_solver
from pbnalog import PbNalogClient
from pbnalog.__main__ import manual_solver as pb_solver
from rnp import RnpClient

from .checker import CompanyChecker


async def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m checks")
    ap.add_argument("query", help="ИНН или ОГРН/ОГРНИП")
    ap.add_argument("-o", "--output", help="сохранить отчёт в JSON-файл")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    async with PbNalogClient(solver=pb_solver) as pb, RnpClient() as rnp, \
            FsspClient(solver=fssp_solver) as fssp:
        report = await CompanyChecker(pb, rnp, fssp).check(args.query)

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"Сохранено в {args.output}")
    else:
        print(text)
    for flag in report["flags"]:
        print("!", flag, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
