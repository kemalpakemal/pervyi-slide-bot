"""Сквозной тест брифа на SQLite с подменённой сессией Telegram (без сети)."""
import asyncio
import datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, Update, User
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "1:x")
    monkeypatch.setenv("ADMIN_ID", "1")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path / "data"))
    import bot

    bot.settings = bot.Settings()
    bot.engine = create_async_engine(bot.settings.db_url)
    bot.Session = async_sessionmaker(bot.engine, expire_on_commit=False, class_=AsyncSession)
    await bot.init_db()
    sent = []

    class FakeSession(BaseSession):
        async def close(self):
            pass

        async def make_request(self, b, method, timeout=None):
            sent.append(method)
            if "Message" in str(method.__returning__):
                return Message(message_id=9, date=datetime.datetime.now(),
                               chat=Chat(id=1, type="private"), text="x")
            return True

        async def stream_content(self, *a, **k):
            yield b""

    b = Bot("1:x", session=FakeSession())
    bot.router._parent_router = None  # роутер модульный: отцепляем от прошлого теста
    dp = Dispatcher(storage=bot.DatabaseStorage())
    dp.include_router(bot.router)
    yield bot, b, dp, sent
    await bot.engine.dispose()


_n = [0]


def msg(text=None, uid=2, photo=False):
    _n[0] += 1
    kw = dict(message_id=_n[0], date=datetime.datetime.now(), chat=Chat(id=uid, type="private"),
              from_user=User(id=uid, is_bot=False, first_name="C"))
    if photo:
        kw["photo"] = [PhotoSize(file_id=f"f{_n[0]}", file_unique_id="u", width=1, height=1, file_size=100)]
    if text:
        kw["text"] = text
    return Update(update_id=_n[0], message=Message(**kw))


def click(uid=2):
    _n[0] += 1
    return Update(update_id=_n[0], callback_query=CallbackQuery(
        id="1", from_user=User(id=uid, is_bot=False, first_name="C"), chat_instance="1",
        data="brief:confirm", message=Message(message_id=5, date=datetime.datetime.now(),
                                               chat=Chat(id=uid, type="private"), text="x")))


async def test_brief_flow_limits_and_double_confirm(env):
    bot, b, dp, sent = env
    for t in ["/start", "Заказать карточку", "WB", "Тапок", "Мягкий", "нет", "минимализм", "синий", "3", "не срочно"]:
        await dp.feed_update(b, msg(t))
    await asyncio.gather(*[dp.feed_update(b, msg(photo=True)) for _ in range(20)])
    await dp.feed_update(b, msg("Готово"))
    await asyncio.gather(dp.feed_update(b, click()), dp.feed_update(b, click()))
    async with bot.Session() as db:
        assert await db.scalar(select(func.count()).select_from(bot.Order)) == 1
        assert await db.scalar(select(func.count()).select_from(bot.OrderFile)) == bot.settings.max_files_per_order


async def test_admin_only(env):
    bot, b, dp, sent = env
    await dp.feed_update(b, msg("/health", uid=2))
    assert not sent
    await dp.feed_update(b, msg("/health", uid=1))
    assert any("БД: OK" in (getattr(m, "text", "") or "") for m in sent)
