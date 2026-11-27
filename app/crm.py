"""Общая логика CRM: клиент для учётки кабинета, журнал, этапы сделки."""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Activity, Check, CheckStatus, Client, Deal, DealStage, User, utcnow


async def client_for_user(db: AsyncSession, user: User) -> Client:
    """Клиент CRM, привязанный к учётке кабинета; заводится при первой заявке."""
    client = (await db.execute(select(Client).where(Client.user_id == user.id))).scalar_one_or_none()
    if client is None:
        client = Client(user_id=user.id, source="Кабинет: подбор закупок")
        db.add(client)
        await db.flush()
        log(db, client.id, None, f"Клиент зарегистрировался в кабинете ({user.email or 'Telegram'})")
    return client


def log(db: AsyncSession, client_id: int, author: User | None, text: str, *,
        deal_id: int | None = None, kind: str = "system") -> None:
    db.add(Activity(client_id=client_id, deal_id=deal_id, author_id=author.id if author else None,
                    kind=kind, text=text))


async def set_stage(db: AsyncSession, deal: Deal, stage: str, author: User | None) -> bool:
    if stage not in DealStage.TITLES or stage == deal.stage:
        return False
    old = deal.stage_title
    deal.stage = stage
    deal.closed_at = utcnow() if stage in DealStage.CLOSED else None
    log(db, deal.client_id, author, f"Сделка №{deal.id}: этап «{old}» → «{deal.stage_title}»", deal_id=deal.id)
    from .outreach import notify_stage
    if await notify_stage(db, deal):
        log(db, deal.client_id, None, f"Сделка №{deal.id}: клиенту отправлено уведомление «{deal.stage_title}»",
            deal_id=deal.id)
    return True


async def client_checks(db: AsyncSession, client: Client, limit: int = 20) -> list[Check]:
    if not client.inn:
        return []
    rows = await db.execute(select(Check).where(or_(Check.inn == client.inn, Check.query == client.inn))
                            .order_by(Check.id.desc()).limit(limit))
    return list(rows.scalars())


async def fill_from_checks(db: AsyncSession, client: Client) -> None:
    """Название и ОГРН клиента подтягиваем из последней завершённой проверки по его ИНН."""
    if not client.inn or (client.title and client.ogrn):
        return
    check = (await db.execute(select(Check).where(Check.inn == client.inn, Check.status == CheckStatus.DONE)
                              .order_by(Check.id.desc()).limit(1))).scalar_one_or_none()
    if check is not None:
        client.title = client.title or check.title
        client.ogrn = client.ogrn or check.ogrn


def client_title(client: Client) -> str:
    if client.title:
        return client.title
    if client.inn:
        return f"ИНН {client.inn}"
    if client.user is not None and client.user.email:
        return client.user.email
    return f"Клиент №{client.id}"
