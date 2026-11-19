"""Загрузка новых извещений из ЕИС по регионам из профилей клиентов.

Раз в сутки (таймер systemd из deploy/install-lite.sh):

    python -m app.tenders_sync                 # за вчера
    python -m app.tenders_sync --date 2026-11-18 --region 77
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from tenders import EisClient, EisError
from tenders.eis import LAWS

from .config import settings
from .db import create_tables, make_engine, make_sessionmaker
from .models import TenderNotice, TenderProfile

log = logging.getLogger(__name__)


async def profile_regions(sm: async_sessionmaker) -> list[str]:
    async with sm() as db:
        rows = (await db.execute(select(TenderProfile.regions))).scalars().all()
    return sorted({r for regions in rows for r in (regions or [])})


async def profile_laws(sm: async_sessionmaker) -> tuple[str, ...]:
    async with sm() as db:
        rows = (await db.execute(select(TenderProfile.laws))).scalars().all()
    wanted = {law for laws in rows for law in (laws if laws is not None else LAWS)}
    return tuple(law for law in LAWS if law in wanted)


async def sync(sm: async_sessionmaker, client: EisClient, day: date,
               regions: list[str] | None = None, laws: tuple[str, ...] | None = None) -> dict[str, int]:
    """Сохраняет извещения за день по регионам. Возвращает {регион: сколько новых/обновлённых}."""
    regions = regions if regions is not None else await profile_regions(sm)
    laws = laws if laws is not None else await profile_laws(sm)
    stats: dict[str, int] = {}
    for region in regions:
        count = 0
        try:
            async with sm() as db:
                async for n in client.notices(region, day, laws):
                    row = (await db.execute(select(TenderNotice).where(
                        TenderNotice.purchase_number == n.purchase_number))).scalar_one_or_none()
                    if row is None:
                        row = TenderNotice(purchase_number=n.purchase_number)
                        db.add(row)
                    # Изменения извещения приходят новым документом с тем же номером: берём последний.
                    row.region, row.ends_at, row.max_price = n.region, n.ends_at, n.max_price
                    row.data = n.to_dict()
                    row.fetched_at = datetime.now(timezone.utc)
                    count += 1
                await db.commit()
        except EisError as e:
            log.error("ЕИС, регион %s: %s", region, e)
        stats[region] = count
    # Закупки, приём заявок по которым закончился неделю назад, больше не нужны.
    async with sm() as db:
        await db.execute(delete(TenderNotice).where(
            TenderNotice.ends_at < datetime.now(timezone.utc) - timedelta(days=7)))
        await db.commit()
    return stats


async def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m app.tenders_sync")
    ap.add_argument("--date", type=date.fromisoformat, default=date.today() - timedelta(days=1))
    ap.add_argument("--region", action="append", help="код региона (по умолчанию — из профилей клиентов)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    engine = make_engine(settings.database_url)
    await create_tables(engine)
    client = EisClient(settings.eis_token)
    try:
        stats = await sync(make_sessionmaker(engine), client, a.date, a.region)
        log.info("Извещения за %s: %s", a.date, stats or "нет регионов в профилях клиентов")
    finally:
        await client.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
