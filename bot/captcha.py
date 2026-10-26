"""Капча через чат: бот присылает картинку и ждёт цифры от того, кто сделал запрос."""

import asyncio
import contextvars

from aiogram import Bot
from aiogram.types import BufferedInputFile

current_chat: contextvars.ContextVar[int] = contextvars.ContextVar("current_chat")


class ChatCaptchaSolver:
    def __init__(self, bot: Bot, timeout: float = 120):
        self.bot = bot
        self.timeout = timeout
        self._waiting: dict[int, asyncio.Future[str]] = {}

    def is_waiting(self, chat_id: int) -> bool:
        return chat_id in self._waiting

    def answer(self, chat_id: int, text: str) -> None:
        fut = self._waiting.get(chat_id)
        if fut and not fut.done():
            fut.set_result(text)

    async def __call__(self, image: bytes) -> str:
        chat_id = current_chat.get()
        fut = asyncio.get_running_loop().create_future()
        self._waiting[chat_id] = fut
        try:
            await self.bot.send_photo(
                chat_id, BufferedInputFile(image, "captcha.png"),
                caption="Сайт ФНС просит капчу. Пришлите цифры с картинки.")
            return await asyncio.wait_for(fut, self.timeout)
        finally:
            self._waiting.pop(chat_id, None)
