"""Запуск: BOT_TOKEN=... python -m bot.main"""

import asyncio
import json
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BufferedInputFile, Message

from checks import CompanyChecker
from fssp import FsspClient
from pbnalog import PbNalogClient, classify
from rnp import RnpClient

from .captcha import ChatCaptchaSolver, current_chat
from .formatting import format_report

log = logging.getLogger(__name__)

HELP = ("Пришлите ИНН или ОГРН компании или ИП, и я проверю её по сервису ФНС "
        "«Прозрачный бизнес», реестру недобросовестных поставщиков и банку данных "
        "исполнительных производств ФССП.")


async def cabinet_post(path: str, telegram_id: int, payload: dict | None = None):
    """Запрос к API кабинета от имени пользователя бота. None — кабинет не настроен или недоступен."""
    import httpx
    base = os.environ.get("CABINET_URL", "http://127.0.0.1:8000").rstrip("/")
    token = os.environ.get("BOT_API_TOKEN", "")
    if not token:
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            return await client.post(f"{base}{path}", json=payload or {},
                                     headers={"X-Bot-Token": token, "X-Telegram-Id": str(telegram_id)})
    except httpx.HTTPError:
        log.exception("кабинет недоступен")
        return None


async def link_account(code: str, telegram_id: int) -> str:
    """Привязка чата к учётке кабинета по коду из ссылки t.me/<бот>?start=link_<код>."""
    r = await cabinet_post("/api/telegram/link", telegram_id, {"code": code})
    if r is None:
        return "Кабинет сейчас недоступен или не настроен, попробуйте позже."
    if r.status_code == 404:
        return "Ссылка устарела. Получите новую в кабинете на странице «Задачи»."
    if r.status_code != 200:
        return "Не получилось привязать, попробуйте позже."
    data = r.json()
    extra = " Сюда будут приходить напоминания по задачам." if data.get("staff") else ""
    return f"Готово, Telegram привязан к кабинету {data.get('email') or ''}.{extra}"


def build_dispatcher(checker: CompanyChecker, solver: ChatCaptchaSolver) -> Dispatcher:
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def start(message: Message, command: CommandObject) -> None:
        if command.args and command.args.startswith("link_"):
            await message.answer(await link_account(command.args[5:], message.chat.id))
            return
        await message.answer(HELP)

    @dp.message(Command("stop"))
    async def stop(message: Message) -> None:
        r = await cabinet_post("/api/telegram/stop", message.chat.id)
        if r is None or r.status_code != 200:
            await message.answer("Не получилось, попробуйте позже.")
            return
        await message.answer("Готово, рекламные сообщения в Telegram больше приходить не будут. "
                             "Уведомления по вашим заявкам это не затрагивает.")

    @dp.message(F.text, lambda m: solver.is_waiting(m.chat.id))
    async def captcha_answer(message: Message) -> None:
        solver.answer(message.chat.id, message.text)

    @dp.message(F.text)
    async def lookup(message: Message) -> None:
        if classify(message.text) is None:
            await message.answer("Это не похоже на корректный ИНН или ОГРН. " + HELP)
            return
        current_chat.set(message.chat.id)
        await message.answer("Проверяю: ФНС, реестр недобросовестных поставщиков, ФССП…")
        report = await checker.check(message.text)
        raw = json.dumps(report, ensure_ascii=False, indent=2).encode()
        await message.answer(format_report(report))
        await message.answer_document(BufferedInputFile(raw, f"{report['inn'] or report['query']}.json"))

    return dp


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    bot = Bot(os.environ["BOT_TOKEN"], default=DefaultBotProperties(parse_mode="HTML"))
    solver = ChatCaptchaSolver(bot)
    fssp_solver = solver.with_caption("Сайт ФССП просит капчу. Пришлите символы с картинки.")
    async with PbNalogClient(solver=solver) as pb, RnpClient() as rnp, \
            FsspClient(solver=fssp_solver) as fssp:
        checker = CompanyChecker(pb, rnp, fssp)
        await build_dispatcher(checker, solver).start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
