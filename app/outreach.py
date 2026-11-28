"""Согласия, уведомления клиентам и рекламные рассылки.

Правила, которые здесь зашиты (закон «О рекламе», ст. 18; 152-ФЗ):
- реклама уходит только на адрес с действующим согласием kind=ads по этому каналу;
- перед отправкой каждого письма согласие проверяется ещё раз (могли отписаться);
- в каждом рекламном сообщении есть пометка «Реклама», рекламодатель и способ отписаться;
- уведомления по собственной заявке клиента рекламой не являются и идут без согласия на рекламу,
  но клиент может их выключить в настройках.
"""

from __future__ import annotations

import asyncio
import html
import logging
import secrets
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .models import Campaign, Client, Consent, Deal, DealStage, Outbox, User, utcnow

log = logging.getLogger(__name__)

CONSENT_VERSION = "2026-11-28"
CHANNEL_TITLES = {"email": "Email", "telegram": "Telegram", "sms": "SMS"}

# Этапы, о которых клиенту сообщаем сами.
NOTIFY_STAGES = {
    DealStage.BANKS: "заявка отправлена в банки",
    DealStage.APPROVED: "банк одобрил гарантию",
    DealStage.ISSUED: "гарантия выдана",
    DealStage.LOST: "по заявке получен отказ, менеджер свяжется с вами",
}


def operator(settings) -> str:
    parts = [settings.operator_name or "оператор сервиса"]
    if settings.operator_inn:
        parts.append(f"ИНН {settings.operator_inn}")
    if settings.operator_address:
        parts.append(settings.operator_address)
    return ", ".join(parts)


def pd_text(settings) -> str:
    return (f"Я даю согласие {operator(settings)} на обработку моих персональных данных (email, телефон, "
            "имя, сведения о компании и сделках) с целью регистрации в личном кабинете, проверки "
            "контрагентов, подбора закупок и оформления банковских гарантий: сбор, запись, хранение на "
            "серверах в России, уточнение, использование, передачу банкам-партнёрам по моей заявке, "
            "удаление. Согласие действует до его отзыва; отозвать его можно письмом оператору или в "
            f"настройках кабинета. Редакция {CONSENT_VERSION}.")


def ads_text(settings, channel: str) -> str:
    return (f"Я согласен получать от {operator(settings)} рекламные и информационные сообщения о банковских "
            f"гарантиях, страховании и услугах сервиса по каналу «{CHANNEL_TITLES.get(channel, channel)}». "
            "Согласие можно отозвать в любой момент: ссылкой «Отписаться» в письме, командой /stop в "
            f"Telegram-боте или в настройках кабинета. Редакция {CONSENT_VERSION}.")


def new_token() -> str:
    return secrets.token_urlsafe(16)[:22]


async def active_consent(db: AsyncSession, kind: str, channel: str, address: str) -> Consent | None:
    return (await db.execute(select(Consent).where(
        Consent.kind == kind, Consent.channel == channel, Consent.address == address,
        Consent.revoked_at.is_(None)).order_by(Consent.id.desc()).limit(1))).scalar_one_or_none()


async def give_consent(db: AsyncSession, settings, *, kind: str, channel: str, address: str, source: str,
                       user: User | None = None, client_id: int | None = None, ip: str | None = None,
                       user_agent: str | None = None, recorded_by: User | None = None,
                       wording: str | None = None) -> Consent:
    existing = await active_consent(db, kind, channel, address)
    if existing is not None:
        return existing
    consent = Consent(kind=kind, channel=channel, address=address, source=source[:300],
                      wording=wording or (pd_text(settings) if kind == "pd" else ads_text(settings, channel)),
                      user_id=user.id if user else None, client_id=client_id, ip=ip,
                      user_agent=(user_agent or "")[:300] or None,
                      recorded_by=recorded_by.id if recorded_by else None, token=new_token())
    db.add(consent)
    await db.flush()
    return consent


async def revoke(db: AsyncSession, consent: Consent, source: str) -> None:
    if consent.revoked_at is None:
        consent.revoked_at = utcnow()
        consent.revoke_source = source[:200]


async def revoke_address(db: AsyncSession, kind: str, channel: str, address: str, source: str) -> int:
    rows = (await db.execute(select(Consent).where(
        Consent.kind == kind, Consent.channel == channel, Consent.address == address,
        Consent.revoked_at.is_(None)))).scalars().all()
    for c in rows:
        await revoke(db, c, source)
    return len(rows)


def user_addresses(user: User) -> dict[str, str]:
    out = {}
    if user.email:
        out["email"] = user.email
    if user.telegram_id:
        out["telegram"] = str(user.telegram_id)
    return out


# --- Уведомления клиенту по его сделке -------------------------------------------------------------

async def notify_stage(db: AsyncSession, deal: Deal) -> int:
    """Ставит в очередь уведомление владельцу кабинета о новом этапе его сделки."""
    what = NOTIFY_STAGES.get(deal.stage)
    if what is None:
        return 0
    client = deal.client if "client" in deal.__dict__ else await db.get(Client, deal.client_id)
    if client is None or client.user_id is None:
        return 0
    user = await db.get(User, client.user_id)
    if user is None or not user.notify:
        return 0
    about = deal.product + (f" по закупке № {deal.purchase_number}" if deal.purchase_number else "")
    text = f"{about}: {what}."
    channel, address = (("telegram", str(user.telegram_id)) if user.telegram_id
                        else ("email", user.email) if user.email else (None, None))
    if channel is None:
        return 0
    db.add(Outbox(kind="service", channel=channel, address=address, client_id=client.id,
                  subject=f"Заявка на гарантию: {what}", body=text))
    return 1


# --- Рассылки ----------------------------------------------------------------------------------------

SEGMENTS = {
    "all": "Все, кто дал согласие",
    "issued": "Клиенты с выданной гарантией",
    "open": "Клиенты со сделкой в работе",
    "none": "Клиенты без сделок",
}


async def recipients(db: AsyncSession, campaign: Campaign) -> list[Consent]:
    """Действующие согласия на рекламу по каналу рассылки, отфильтрованные по сегменту, по одному на адрес."""
    seg = campaign.segment or {}
    rows = (await db.execute(select(Consent).where(
        Consent.kind == "ads", Consent.channel == campaign.channel, Consent.revoked_at.is_(None))
        .order_by(Consent.id))).scalars().all()
    # Клиент согласия: явно указанный или привязанный к учётке, которая дала согласие.
    by_user = {c.user_id: c.id for c in (await db.execute(select(Client).where(Client.user_id.is_not(None)))).scalars()}
    client_ids = {c.id: c.client_id or by_user.get(c.user_id) for c in rows}
    want = seg.get("group", "all")
    source = (seg.get("source") or "").strip().lower()
    stages: dict[int, set[str]] = {}
    sources: dict[int, str] = {}
    if want != "all" or source:
        for d in (await db.execute(select(Deal.client_id, Deal.stage))).all():
            stages.setdefault(d.client_id, set()).add(d.stage)
        for cid, src in (await db.execute(select(Client.id, Client.source))).all():
            sources[cid] = (src or "").lower()
    out, seen = [], set()
    for c in rows:
        if c.address in seen:
            continue
        cid = client_ids.get(c.id)
        st = stages.get(cid, set())
        if want == "issued" and DealStage.ISSUED not in st:
            continue
        if want == "open" and not (st & set(DealStage.OPEN)):
            continue
        if want == "none" and st:
            continue
        if source and source not in sources.get(cid, ""):
            continue
        seen.add(c.address)
        out.append(c)
    return out


def ad_footer(campaign: Campaign, settings, consent: Consent, public_url: str) -> str:
    who = campaign.advertiser or operator(settings)
    lines = [f"Реклама. {who}" + (f". erid: {campaign.erid}" if campaign.erid else "")]
    if campaign.channel == "telegram":
        lines.append("Отписаться от рассылки: /stop")
    else:
        lines.append(f"Отписаться: {public_url or '<адрес кабинета>'}/u/{consent.token}")
    return "\n".join(lines)


async def queue_campaign(db: AsyncSession, campaign: Campaign, settings, public_url: str) -> int:
    targets = await recipients(db, campaign)
    for consent in targets:
        body = f"{campaign.body.strip()}\n\n{ad_footer(campaign, settings, consent, public_url)}"
        db.add(Outbox(kind="ads", channel=campaign.channel, address=consent.address, subject=campaign.subject,
                      body=body, campaign_id=campaign.id, consent_id=consent.id, client_id=consent.client_id))
    campaign.status = "queued"
    campaign.queued_at = utcnow()
    return len(targets)


# --- Доставка ----------------------------------------------------------------------------------------

class Mailer:
    """SMTP с TLS (порт 465) или STARTTLS (587). Отправка в отдельном потоке."""

    def __init__(self, settings):
        self.s = settings

    @property
    def ready(self) -> bool:
        return bool(self.s.smtp_host and self.s.smtp_from)

    def _send(self, to: str, subject: str, body: str, unsubscribe: str | None) -> None:
        msg = EmailMessage()
        msg["From"] = formataddr((self.s.operator_name or "", self.s.smtp_from))
        msg["To"] = to
        msg["Subject"] = subject
        msg["Message-ID"] = make_msgid()
        if unsubscribe:
            msg["List-Unsubscribe"] = f"<{unsubscribe}>"
            msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        msg.set_content(body)
        ctx = ssl.create_default_context()
        if self.s.smtp_port == 465:
            server = smtplib.SMTP_SSL(self.s.smtp_host, self.s.smtp_port, context=ctx, timeout=30)
        else:
            server = smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=30)
            server.starttls(context=ctx)
        with server:
            if self.s.smtp_user:
                server.login(self.s.smtp_user, self.s.smtp_password)
            server.send_message(msg)

    async def send(self, to: str, subject: str, body: str, unsubscribe: str | None = None) -> None:
        await asyncio.to_thread(self._send, to, subject, body, unsubscribe)


async def deliver(sessionmaker, *, telegram=None, mailer: Mailer | None = None, public_url: str = "",
                  limit: int = 500) -> dict:
    """Отправляет очередь. telegram(chat_id, text) — функция отправки в бота (см. crm_tasks)."""
    stats = {"sent": 0, "failed": 0, "skipped": 0}
    async with sessionmaker() as db:
        items = (await db.execute(select(Outbox).where(Outbox.status == "queued")
                                  .order_by(Outbox.id).limit(limit))).scalars().all()
        for item in items:
            if item.kind == "ads":
                consent = await db.get(Consent, item.consent_id) if item.consent_id else None
                if consent is None or consent.revoked_at is not None:
                    item.status, item.error = "skipped", "согласие отозвано"
                    stats["skipped"] += 1
                    continue
            try:
                if item.channel == "telegram":
                    if telegram is None:
                        continue                  # подождём, пока настроят BOT_TOKEN
                    text = html.escape(item.body)
                    await telegram(int(item.address), text)
                    await asyncio.sleep(0.05)     # лимит Telegram: не больше ~30 сообщений в секунду
                elif item.channel == "email":
                    if mailer is None or not mailer.ready:
                        continue
                    unsub = None
                    if item.kind == "ads" and public_url and item.consent_id:
                        consent = await db.get(Consent, item.consent_id)
                        unsub = f"{public_url}/u/{consent.token}"
                    await mailer.send(item.address, item.subject or "Сообщение", item.body, unsub)
                else:
                    item.status, item.error = "skipped", f"канал {item.channel} не поддерживается"
                    stats["skipped"] += 1
                    continue
                item.status, item.sent_at = "sent", utcnow()
                stats["sent"] += 1
            except Exception as e:
                log.exception("не отправилось сообщение %s", item.id)
                item.status, item.error = "failed", str(e)[:1000]
                stats["failed"] += 1
        await db.flush()
        pending = select(Outbox.id).where(Outbox.campaign_id == Campaign.id, Outbox.status == "queued").exists()
        for c in (await db.execute(select(Campaign).where(Campaign.status == "queued", ~pending))).scalars():
            c.status = "sent"
        await db.commit()
    return stats


async def campaign_stats(db: AsyncSession, campaign_ids: list[int]) -> dict[int, dict[str, int]]:
    from sqlalchemy import func
    out: dict[int, dict[str, int]] = {}
    if not campaign_ids:
        return out
    rows = await db.execute(select(Outbox.campaign_id, Outbox.status, func.count())
                            .where(Outbox.campaign_id.in_(campaign_ids)).group_by(Outbox.campaign_id, Outbox.status))
    for cid, status, n in rows.all():
        out.setdefault(cid, {})[status] = n
    return out


async def client_consents(db: AsyncSession, client: Client) -> list[Consent]:
    from sqlalchemy import or_
    conds = [Consent.client_id == client.id]
    if client.user_id:
        conds.append(Consent.user_id == client.user_id)
    return list((await db.execute(select(Consent).where(or_(*conds)).order_by(Consent.id.desc()))).scalars())


