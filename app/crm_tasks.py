"""Задачи сотрудников, напоминания в Telegram и отправка очереди сообщений (уведомления, рассылки).

Раз в несколько минут: python -m app.crm_tasks (на сервере это делает таймер systemd или воркер arq).
Заводит задачи «гарантия истекает» и отправляет напоминания по наступившим задачам.
"""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .models import STAFF_ROLES, Client, Deal, DealStage, Task, User, utcnow

log = logging.getLogger(__name__)

MSK = ZoneInfo("Europe/Moscow")
RENEW_DAYS = 30           # за сколько дней до конца гарантии ставить задачу
DEFAULT_TIME = time(10, 0)

Send = Callable[[int, str], Awaitable[None]]


def aware(dt: datetime | None) -> datetime | None:
    """SQLite отдаёт время без пояса; в базе оно всегда в UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def msk(dt: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    return aware(dt).astimezone(MSK).strftime(fmt) if dt else ""


def parse_due(raw: str) -> datetime | None:
    """Поле формы: «2026-11-28T15:30» (datetime-local) или «2026-11-28», время московское."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        if "T" in raw or " " in raw:
            local = datetime.fromisoformat(raw.replace(" ", "T"))
        else:
            local = datetime.combine(date.fromisoformat(raw), DEFAULT_TIME)
    except ValueError:
        return None
    return local.replace(tzinfo=MSK).astimezone(timezone.utc)


def parse_date(raw: str) -> date | None:
    try:
        return date.fromisoformat((raw or "").strip()) if (raw or "").strip() else None
    except ValueError:
        return None


async def create_renewal_tasks(db, now: datetime) -> int:
    today = now.astimezone(MSK).date()
    rows = await db.execute(select(Deal).options(selectinload(Deal.client)).where(
        Deal.stage == DealStage.ISSUED, Deal.expires_at.is_not(None),
        Deal.expires_at <= today + timedelta(days=RENEW_DAYS)))
    created = 0
    for deal in rows.scalars():
        key = f"renew:{deal.id}:{deal.expires_at.isoformat()}"
        if (await db.execute(select(Task.id).where(Task.auto_key == key))).first():
            continue
        db.add(Task(title=f"Гарантия по сделке №{deal.id} заканчивается {deal.expires_at:%d.%m.%Y}: "
                          f"предложить клиенту новую", client_id=deal.client_id, deal_id=deal.id,
                    assignee_id=deal.client.manager_id, due_at=now, auto_key=key))
        created += 1
    return created


def reminder_text(task: Task, public_url: str = "") -> str:
    lines = [f"⏰ <b>{html.escape(task.title)}</b>"]
    if task.client is not None:
        from .crm import client_title
        lines.append(f"Клиент: {html.escape(client_title(task.client))}")
    if task.due_at:
        lines.append(f"Срок: {msk(task.due_at)}")
    if public_url:
        lines.append(f"{public_url}/crm/tasks")
    return "\n".join(lines)


async def send_reminders(sessionmaker, send: Send | None, public_url: str = "",
                         now: datetime | None = None) -> dict:
    now = now or utcnow()
    sent = 0
    async with sessionmaker() as db:
        created = await create_renewal_tasks(db, now)
        await db.flush()
        staff_tg = [u.telegram_id for u in (await db.execute(select(User).where(
            User.role.in_(STAFF_ROLES), User.telegram_id.is_not(None)))).scalars()]
        due = await db.execute(select(Task).options(
            selectinload(Task.assignee), selectinload(Task.client).selectinload(Client.user)).where(
            Task.remind.is_(True), Task.done_at.is_(None), Task.reminded_at.is_(None),
            Task.due_at.is_not(None), Task.due_at <= now))
        for task in due.scalars():
            if send is None:
                chats = []
            elif task.assignee is not None:
                chats = [task.assignee.telegram_id] if task.assignee.telegram_id else []
            else:
                chats = staff_tg
            text = reminder_text(task, public_url)
            for chat in chats:
                try:
                    await send(chat, text)
                    sent += 1
                except Exception:       # заблокировал бота, сеть: не мешаем остальным
                    log.exception("не отправилось напоминание %s в чат %s", task.id, chat)
            task.reminded_at = now
        await db.commit()
    return {"created": created, "sent": sent}


def telegram_sender(token: str) -> tuple[Send, Callable[[], Awaitable[None]]]:
    import httpx
    client = httpx.AsyncClient(base_url=f"https://api.telegram.org/bot{token}/", timeout=20)

    async def send(chat_id: int, text: str) -> None:
        r = await client.post("sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                                   "disable_web_page_preview": True})
        r.raise_for_status()

    return send, client.aclose


async def main() -> None:
    from .config import settings
    from .db import create_tables, make_engine, make_sessionmaker
    logging.basicConfig(level=logging.INFO)
    engine = make_engine(settings.database_url)
    await create_tables(engine)
    if settings.bot_token:
        send, close = telegram_sender(settings.bot_token)
    else:
        log.warning("BOT_TOKEN не задан: задачи заводятся, напоминания в Telegram не отправляются")
        send = None

        async def close() -> None:
            pass
    from .outreach import Mailer, deliver
    sm = make_sessionmaker(engine)
    try:
        print(await send_reminders(sm, send, settings.public_url))
        print(await deliver(sm, telegram=send, mailer=Mailer(settings), public_url=settings.public_url))
    finally:
        await close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
