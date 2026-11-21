"""Сквозной тест маршрутизации: реальный Dispatcher, поддельный Telegram API (без сети)."""
import datetime
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("BOT_TOKEN", "123456:testing-token")
os.environ.setdefault("ADMIN_ID", "1")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./bot-workflow-import.db")

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, Update, User
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import bot as app

ADMIN = 1
CLIENT = 5


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.temp.name) / 'd.db'}")
        self.original_session = app.Session
        app.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(app.Base.metadata.create_all)

        self.calls: list[tuple[int, str]] = []
        calls = self.calls

        async def fake_request(session, bot, method, timeout=None):
            if isinstance(method, SendMessage):
                calls.append((method.chat_id, method.text))
                return Message(
                    message_id=1, date=datetime.datetime.now(),
                    chat=Chat(id=method.chat_id, type="private"), text=method.text,
                )
            return True

        self.original_request = AiohttpSession.make_request
        AiohttpSession.make_request = fake_request
        self.bot = Bot("123456:testing-token")
        self.dp = Dispatcher(storage=app.DatabaseStorage())
        self.dp.include_router(app.router)
        self.counter = 0

    async def asyncTearDown(self):
        AiohttpSession.make_request = self.original_request
        app.Session = self.original_session
        app.router._parent_router = None  # роутер модульный: отцепляем для других тестов
        await self.bot.session.close()
        await self.engine.dispose()
        self.temp.cleanup()

    async def send(self, user_id: int, text: str):
        self.counter += 1
        update = Update(update_id=self.counter, message=Message(
            message_id=self.counter, date=datetime.datetime.now(),
            chat=Chat(id=user_id, type="private"),
            from_user=User(id=user_id, is_bot=False, first_name="T"), text=text,
        ))
        await self.dp.feed_update(self.bot, update)

    async def state(self, user_id: int):
        return await self.dp.storage.get_state(StorageKey(bot_id=123456, chat_id=user_id, user_id=user_id))

    def last_text(self, chat_id: int) -> str:
        return [text for cid, text in self.calls if cid == chat_id][-1]

    async def test_cancel_command_works_inside_brief(self):
        await self.send(CLIENT, "Заказать карточку")
        await self.send(CLIENT, "WB")
        await self.send(CLIENT, "/cancel")
        self.assertIsNone(await self.state(CLIENT))

    async def test_every_admin_button_answers_the_owner(self):
        buttons = [
            "📥 Все заявки", "🕒 На проверке", "🎨 На генерации", "👁 На согласовании", "💰 Оплаты",
            "👥 Клиенты", "⚙️ Настройки", "📨 Напоминания", "📊 Статистика", "⚙️ Состояние бота",
        ]
        for button in buttons:
            before = len(self.calls)
            await self.send(ADMIN, button)
            self.assertGreater(len(self.calls), before, f"кнопка «{button}» не ответила владельцу")

    async def test_non_owner_cannot_use_admin_buttons(self):
        await self.send(CLIENT, "📊 Статистика")
        self.assertIn("только владельцу", self.last_text(CLIENT))

    async def test_menu_button_drops_pending_broadcast(self):
        await self.send(ADMIN, "📣 Рассылка")
        self.assertEqual(await self.state(ADMIN), "AdminAction:broadcast")
        await self.send(ADMIN, "📊 Статистика")
        self.assertIsNone(await self.state(ADMIN))

    async def test_commands_are_not_forwarded_as_clarification(self):
        async with app.Session() as db:
            client = app.Client(telegram_id=CLIENT)
            db.add(client)
            await db.flush()
            db.add(app.Order(id="clarify-order-1", client_id=client.id, status="admin_review", brief={}))
            await db.commit()
        key = StorageKey(bot_id=123456, chat_id=ADMIN, user_id=ADMIN)
        await self.dp.storage.set_state(key, app.AdminAction.clarification.state)
        await self.dp.storage.set_data(key, {"order_id": "clarify-order-1"})
        await self.send(ADMIN, "/health")
        self.assertEqual([c for c in self.calls if c[0] == CLIENT], [])
        self.assertIn("Бот отвечает", self.last_text(ADMIN))

    async def test_unknown_command_gets_reply(self):
        await self.send(CLIENT, "/nonsense")
        self.assertIn("не поддерживается", self.last_text(CLIENT))
        await self.send(ADMIN, "/nonsense")
        self.assertIn("Неизвестная команда", self.last_text(ADMIN))


if __name__ == "__main__":
    unittest.main()
