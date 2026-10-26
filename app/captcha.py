"""Передача капчи от фоновой проверки пользователю (в кабинет или в бот) и обратно.

Проверка вызывает ask(): картинка сохраняется, статус проверки становится
captcha, и задача ждёт ответа. Кабинет или бот забирают картинку через
image() и присылают цифры через answer().
"""

from __future__ import annotations

import asyncio
from typing import Protocol


class CaptchaTimeout(Exception):
    pass


class CaptchaBroker(Protocol):
    async def ask(self, check_id: int, image: bytes, timeout: float) -> str: ...
    async def image(self, check_id: int) -> bytes | None: ...
    async def answer(self, check_id: int, text: str) -> bool: ...


class MemoryCaptchaBroker:
    """Для запуска без Redis: проверки идут в том же процессе, что и веб-сервер."""

    def __init__(self) -> None:
        self._waiting: dict[int, tuple[bytes, asyncio.Future[str]]] = {}

    async def ask(self, check_id: int, image: bytes, timeout: float) -> str:
        fut = asyncio.get_running_loop().create_future()
        self._waiting[check_id] = (image, fut)
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError as e:
            raise CaptchaTimeout from e
        finally:
            self._waiting.pop(check_id, None)

    async def image(self, check_id: int) -> bytes | None:
        item = self._waiting.get(check_id)
        return item[0] if item else None

    async def answer(self, check_id: int, text: str) -> bool:
        item = self._waiting.get(check_id)
        if not item or item[1].done():
            return False
        item[1].set_result(text)
        return True


class RedisCaptchaBroker:
    """Веб-сервер и воркер в разных процессах: картинка и ответ идут через Redis."""

    def __init__(self, redis) -> None:
        self.redis = redis

    async def ask(self, check_id: int, image: bytes, timeout: float) -> str:
        img_key, ans_key = f"captcha:{check_id}:img", f"captcha:{check_id}:ans"
        await self.redis.delete(ans_key)
        await self.redis.set(img_key, image, ex=int(timeout) + 30)
        try:
            item = await self.redis.blpop([ans_key], timeout=timeout)
        finally:
            await self.redis.delete(img_key)
        if item is None:
            raise CaptchaTimeout
        value = item[1]
        return value.decode() if isinstance(value, bytes) else value

    async def image(self, check_id: int) -> bytes | None:
        return await self.redis.get(f"captcha:{check_id}:img")

    async def answer(self, check_id: int, text: str) -> bool:
        if not await self.redis.exists(f"captcha:{check_id}:img"):
            return False
        ans_key = f"captcha:{check_id}:ans"
        await self.redis.rpush(ans_key, text)
        await self.redis.expire(ans_key, 60)
        return True
