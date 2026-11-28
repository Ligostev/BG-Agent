"""Настройки уведомлений и согласий, тексты согласий, отписка по ссылке, привязка Telegram."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .deps import get_db, session_user, web_user
from .models import Consent, User
from .outreach import (CHANNEL_TITLES, active_consent, ads_text, give_consent, pd_text, revoke, revoke_address,
                       user_addresses)
from .web import go, render

router = APIRouter(include_in_schema=False)


@router.get("/legal/pd")
async def legal_pd(request: Request, user: User | None = Depends(session_user)):
    return render(request, "legal.html", user=user, title="Согласие на обработку персональных данных",
                  text=pd_text(request.app.state.settings))


@router.get("/legal/ads")
async def legal_ads(request: Request, user: User | None = Depends(session_user)):
    return render(request, "legal.html", user=user, title="Согласие на получение рекламы",
                  text=ads_text(request.app.state.settings, "email"))


@router.get("/settings")
async def settings_page(request: Request, user: User = Depends(web_user), db: AsyncSession = Depends(get_db)):
    channels = []
    for channel, address in user_addresses(user).items():
        consent = await active_consent(db, "ads", channel, address)
        channels.append((channel, CHANNEL_TITLES[channel], address, consent))
    s = request.app.state.settings
    return render(request, "settings.html", user=user, channels=channels, bot_username=s.bot_username,
                  bot_ready=bool(s.bot_token))


@router.post("/settings/notify")
async def settings_notify(on: str = Form(""), user: User = Depends(web_user), db: AsyncSession = Depends(get_db)):
    user.notify = bool(on)
    await db.commit()
    return go("/settings")


@router.post("/settings/ads/{channel}")
async def settings_ads(channel: str, request: Request, on: str = Form(""), user: User = Depends(web_user),
                       db: AsyncSession = Depends(get_db)):
    address = user_addresses(user).get(channel)
    if address is None:
        raise HTTPException(400, "Этот канал не подключён")
    if on:
        await give_consent(db, request.app.state.settings, kind="ads", channel=channel, address=address,
                           source="Настройки кабинета: включено «Получать предложения»", user=user,
                           ip=request.client.host if request.client else None,
                           user_agent=request.headers.get("user-agent"))
    else:
        await revoke_address(db, "ads", channel, address, "Настройки кабинета")
    await db.commit()
    return go("/settings")


@router.post("/telegram/link")
async def telegram_link(back: str = Form("/settings"), user: User = Depends(web_user),
                        db: AsyncSession = Depends(get_db)):
    user.telegram_code = secrets.token_urlsafe(12)
    await db.commit()
    return go(back if back in ("/settings", "/crm/tasks") else "/settings")


async def consent_by_token(db: AsyncSession, token: str) -> Consent:
    consent = (await db.execute(select(Consent).where(Consent.token == token))).scalar_one_or_none()
    if consent is None or consent.kind != "ads":
        raise HTTPException(404, "Ссылка не найдена")
    return consent


@router.get("/u/{token}")
async def unsubscribe_page(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    # GET ничего не меняет: почтовые сканеры открывают ссылки сами.
    consent = await consent_by_token(db, token)
    return render(request, "unsubscribe.html", user=None, consent=consent, done=False)


@router.post("/u/{token}")
async def unsubscribe(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    consent = await consent_by_token(db, token)
    await revoke_address(db, "ads", consent.channel, consent.address, "Ссылка «Отписаться» в письме")
    await db.commit()
    return render(request, "unsubscribe.html", user=None, consent=consent, done=True)
