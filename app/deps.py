"""Зависимости FastAPI: сессия БД, текущий пользователь, постановка проверки в очередь."""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from pbnalog import classify

from .models import Check, CheckStatus, User


class LoginRequired(Exception):
    """Перехватывается в main.py и превращается в редирект на /login."""


async def get_db(request: Request):
    async with request.app.state.sessionmaker() as db:
        yield db


async def session_user(request: Request, db: AsyncSession = Depends(get_db)) -> User | None:
    uid = request.session.get("uid")
    return await db.get(User, uid) if uid else None


async def web_user(user: User | None = Depends(session_user)) -> User:
    if user is None:
        raise LoginRequired
    return user


async def staff_user(user: User = Depends(web_user)) -> User:
    if not user.is_staff:
        raise HTTPException(403, "Раздел только для сотрудников")
    return user


async def api_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(session_user),
    x_bot_token: str | None = Header(default=None),
    x_telegram_id: int | None = Header(default=None),
) -> User:
    """Кабинет ходит в API с cookie-сессией, бот с общим токеном и telegram_id пользователя."""
    if user is not None:
        return user
    token = request.app.state.settings.bot_api_token
    if token and x_bot_token == token and x_telegram_id:
        user = (await db.execute(select(User).where(User.telegram_id == x_telegram_id))).scalar_one_or_none()
        if user is None:
            user = User(telegram_id=x_telegram_id)
            db.add(user)
            await db.commit()
        return user
    raise HTTPException(401, "Нужна авторизация")


def normalize_query(raw: str) -> str | None:
    q = "".join(ch for ch in raw if ch.isdigit())
    return q if classify(q) else None


async def create_check(request: Request, db: AsyncSession, user: User, query: str) -> Check:
    check = Check(user_id=user.id, query=query)
    db.add(check)
    await db.commit()
    await request.app.state.enqueue(check.id)
    return check


async def own_check(check_id: int, user: User, db: AsyncSession) -> Check:
    check = await db.get(Check, check_id, options=[selectinload(Check.results)], populate_existing=True)
    if check is None or (check.user_id != user.id and not user.is_staff):
        raise HTTPException(404, "Проверка не найдена")
    return check


async def answer_captcha(request: Request, db: AsyncSession, check: Check, answer: str) -> bool:
    if not await request.app.state.captcha.answer(check.id, answer.strip()):
        return False
    # Сразу снимаем статус, чтобы страница не показала старую капчу ещё раз. Только если
    # проверка всё ещё ждёт капчу: иначе можно затереть уже записанный воркером DONE.
    await db.execute(update(Check).where(Check.id == check.id, Check.status == CheckStatus.CAPTCHA)
                     .values(status=CheckStatus.RUNNING))
    await db.commit()
    return True
