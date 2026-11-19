"""Проверка доступа к ЕИС с сервера:

    EIS_TOKEN=... python -m tenders --region 77 --date 2026-11-18 --limit 5

Печатает найденные извещения. С --raw сохраняет ответ сервиса и архивы в папку,
чтобы сверить формат, если что-то не разбирается.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from datetime import date, timedelta
from pathlib import Path

from .eis import NOTICE_TYPES, EisClient, build_region_request, parse_response


async def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m tenders")
    ap.add_argument("--region", required=True, help="код субъекта, например 77")
    ap.add_argument("--date", type=date.fromisoformat, default=date.today() - timedelta(days=1))
    ap.add_argument("--type", action="append", help=f"вид документа (по умолчанию {', '.join(NOTICE_TYPES)})")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--raw", type=Path, help="папка для сырых ответов и архивов")
    a = ap.parse_args()
    client = EisClient(os.environ.get("EIS_TOKEN", ""))
    types = tuple(a.type or NOTICE_TYPES)
    try:
        if a.raw:
            a.raw.mkdir(parents=True, exist_ok=True)
            for t in types:
                body = build_region_request(a.region, t, a.date).replace("{token}", client.token)
                r = await client.http.post(client.url, content=body.encode(),
                                           headers={"Content-Type": "text/xml; charset=utf-8"})
                (a.raw / f"{t}.response.xml").write_bytes(r.content)
                print(t, "HTTP", r.status_code)
                for i, url in enumerate(parse_response(r.content)):
                    (a.raw / f"{t}.{i}.zip").write_bytes(await client.download(url))
            return
        shown = 0
        async for n in client.notices(a.region, a.date, types):
            print(f"{n.purchase_number}  {n.max_price or 0:>15,.2f}  до {n.ends_at:%d.%m %H:%M}  "
                  f"{','.join(n.okpd2[:3])}  {n.subject[:70]}" if n.ends_at else
                  f"{n.purchase_number}  {n.subject[:90]}")
            shown += 1
            if shown >= a.limit:
                break
        print(f"Показано извещений: {shown}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
