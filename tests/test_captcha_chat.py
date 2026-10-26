import asyncio

from bot.captcha import ChatCaptchaSolver, current_chat


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_photo(self, chat_id, photo, caption=None):
        self.sent.append(chat_id)


def test_solver_waits_for_answer_from_same_chat():
    async def go():
        bot = FakeBot()
        solver = ChatCaptchaSolver(bot, timeout=5)
        current_chat.set(77)
        task = asyncio.create_task(solver(b"img"))
        await asyncio.sleep(0)
        assert solver.is_waiting(77) and not solver.is_waiting(1)
        solver.answer(77, "123456")
        assert await task == "123456"
        assert bot.sent == [77] and not solver.is_waiting(77)

    asyncio.run(go())
