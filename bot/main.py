"""Запуск: BOT_TOKEN=... python -m bot.main"""

import asyncio
import json
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile, Message

from pbnalog import CaptchaRequired, NotFound, PbNalogClient, PbNalogError, classify

from .captcha import ChatCaptchaSolver, current_chat
from .formatting import format_result

log = logging.getLogger(__name__)

HELP = ("Пришлите ИНН или ОГРН компании или ИП, и я покажу сведения "
        "из сервиса ФНС «Прозрачный бизнес».")


def build_dispatcher(bot: Bot, pb: PbNalogClient, solver: ChatCaptchaSolver) -> Dispatcher:
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def start(message: Message) -> None:
        await message.answer(HELP)

    @dp.message(F.text, lambda m: solver.is_waiting(m.chat.id))
    async def captcha_answer(message: Message) -> None:
        solver.answer(message.chat.id, message.text)

    @dp.message(F.text)
    async def lookup(message: Message) -> None:
        if classify(message.text) is None:
            await message.answer("Это не похоже на корректный ИНН или ОГРН. " + HELP)
            return
        current_chat.set(message.chat.id)
        await message.answer("Ищу в базе ФНС…")
        try:
            result = await pb.lookup(message.text)
        except NotFound:
            await message.answer("В «Прозрачном бизнесе» ничего не найдено.")
            return
        except (CaptchaRequired, asyncio.TimeoutError):
            await message.answer("Капча не пройдена, попробуйте ещё раз.")
            return
        except PbNalogError:
            log.exception("ошибка pb.nalog.ru")
            await message.answer("Сайт ФНС ответил ошибкой, попробуйте позже.")
            return
        raw = json.dumps(result, ensure_ascii=False, indent=2).encode()
        await message.answer(format_result(result))
        await message.answer_document(BufferedInputFile(raw, f"{result['hit'].get('inn', 'card')}.json"))

    return dp


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    bot = Bot(os.environ["BOT_TOKEN"], default=DefaultBotProperties(parse_mode="HTML"))
    solver = ChatCaptchaSolver(bot)
    async with PbNalogClient(solver=solver) as pb:
        await build_dispatcher(bot, pb, solver).start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
