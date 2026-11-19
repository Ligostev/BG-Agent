"""Загрузка новых извещений из ЕИС по регионам из профилей клиентов и расчёт векторов.

Таймеры systemd из deploy/install-lite.sh:

    python -m app.tenders_sync                 # раз в сутки: извещения за вчера + векторы
    python -m app.tenders_sync --profiles-only # каждые 15 минут: векторы изменённых описаний клиентов
    python -m app.tenders_sync --date 2026-11-18 --region 77
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from tenders import EisClient, EisError
from tenders.eis import LAWS
from tenders.notice import Notice
from tenders.semantic import Embedder, FastEmbedder, clip

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
                    if row.data != n.to_dict():
                        row.data, row.embedding = n.to_dict(), None
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


def stale_profile(row: TenderProfile) -> bool:
    return (row.description or "") != (row.embedding_text or "")


async def embed_pending(sm: async_sessionmaker, get_embedder: Callable[[], Embedder],
                        notices: bool = True, batch: int = 64) -> dict[str, int]:
    """Векторы для изменённых описаний клиентов и (если notices) для новых закупок.

    Модель загружается, только если есть что считать.
    """
    embedder: Embedder | None = None
    done = {"profiles": 0, "notices": 0}
    async with sm() as db:
        profiles = [p for p in (await db.execute(select(TenderProfile))).scalars() if stale_profile(p)]
        if profiles:
            embedder = embedder or get_embedder()
            with_text = [p for p in profiles if (p.description or "").strip()]
            vectors = embedder.embed([clip(p.description) for p in with_text]) if with_text else []
            for p, v in zip(with_text, vectors):
                p.embedding = v
            for p in profiles:
                if not (p.description or "").strip():
                    p.embedding = None
                p.embedding_text = p.description or ""
            done["profiles"] = len(profiles)
            await db.commit()
        if not notices:
            return done
        now = datetime.now(timezone.utc)
        while True:
            rows = (await db.execute(select(TenderNotice).where(
                TenderNotice.embedding.is_(None),
                (TenderNotice.ends_at.is_(None)) | (TenderNotice.ends_at > now)).limit(batch))).scalars().all()
            if not rows:
                break
            embedder = embedder or get_embedder()
            vectors = embedder.embed([clip(Notice.from_dict(r.data).text) for r in rows])
            for r, v in zip(rows, vectors):
                r.embedding = v
            done["notices"] += len(rows)
            await db.commit()
    return done


async def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m app.tenders_sync")
    ap.add_argument("--date", type=date.fromisoformat, default=date.today() - timedelta(days=1))
    ap.add_argument("--region", action="append", help="код региона (по умолчанию — из профилей клиентов)")
    ap.add_argument("--profiles-only", action="store_true", help="только векторы описаний клиентов, без ЕИС")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    engine = make_engine(settings.database_url)
    await create_tables(engine)
    sm = make_sessionmaker(engine)

    def get_embedder() -> Embedder:
        log.info("Загружаю модель %s", settings.embed_model)
        return FastEmbedder(settings.embed_model, cache_dir=settings.embed_cache_dir or None)

    try:
        if not a.profiles_only:
            client = EisClient(settings.eis_token)
            try:
                stats = await sync(sm, client, a.date, a.region)
                log.info("Извещения за %s: %s", a.date, stats or "нет регионов в профилях клиентов")
            finally:
                await client.aclose()
        if settings.embed_model:
            done = await embed_pending(sm, get_embedder, notices=not a.profiles_only)
            if any(done.values()):
                log.info("Векторы: %s", done)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
