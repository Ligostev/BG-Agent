"""Назначить роль пользователю кабинета: python -m app.roles email@example.ru manager

Роли: client (клиент, по умолчанию), manager (сотрудник), admin (сотрудник с полным доступом).
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import func, select

from .config import settings
from .db import create_tables, make_engine, make_sessionmaker
from .models import User

ROLES = ("client", "manager", "admin")


async def set_role(sessionmaker, email: str, role: str) -> bool:
    if role not in ROLES:
        raise ValueError(f"роль должна быть одной из: {', '.join(ROLES)}")
    async with sessionmaker() as db:
        user = (await db.execute(select(User).where(func.lower(User.email) == email.strip().lower()))
                ).scalar_one_or_none()
        if user is None:
            return False
        user.role = role
        await db.commit()
        return True


async def main(email: str, role: str) -> int:
    engine = make_engine(settings.database_url)
    await create_tables(engine)
    try:
        ok = await set_role(make_sessionmaker(engine), email, role)
    finally:
        await engine.dispose()
    print("Готово" if ok else "Пользователь с таким email не найден, пусть сначала зарегистрируется")
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2])))
