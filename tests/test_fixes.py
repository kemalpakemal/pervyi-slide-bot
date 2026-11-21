import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("BOT_TOKEN", "123456:testing-token")
os.environ.setdefault("ADMIN_ID", "1")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./bot-workflow-import.db")

from aiogram.fsm.storage.base import StorageKey
from aiogram.types import FSInputFile
from PIL import Image
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import bot
import generation


class PureHelperTests(unittest.TestCase):
    def test_split_text_respects_telegram_limit(self):
        text = "\n".join(f"строка {i} " + "x" * 90 for i in range(200))
        chunks = bot.split_text(text, 4000)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 4000 for chunk in chunks))
        self.assertEqual("\n".join(chunks), text)

    def test_split_text_handles_single_giant_line(self):
        chunks = bot.split_text("a" * 9000, 4000)
        self.assertEqual([len(c) for c in chunks], [4000, 4000, 1000])

    def test_parse_price_lines_keeps_dashes_in_name(self):
        items = bot.parse_price_lines("Комплект — 2 слайда — 1 290 ₽\nWi-Fi карточка - 500\nОбложка – 790 руб.")
        self.assertEqual(items, [
            {"name": "Комплект — 2 слайда", "price": 1290},
            {"name": "Wi-Fi карточка", "price": 500},
            {"name": "Обложка", "price": 790},
        ])

    def test_parse_price_lines_rejects_garbage(self):
        for bad in ("", "без суммы", "Товар — много", "Товар — -5"):
            with self.assertRaises(ValueError):
                bot.parse_price_lines(bad)

    def test_bridge_token_must_be_long(self):
        with self.assertRaises(ValueError):
            bot.Settings(bot_token="1:x", admin_id=1, hermes_bridge_token="short")
        self.assertIsNone(bot.Settings(bot_token="1:x", admin_id=1, hermes_bridge_token="  ").hermes_bridge_token)

    def test_bridge_auth_does_not_crash_on_non_ascii_header(self):
        original = bot.settings.hermes_bridge_token
        bot.settings.hermes_bridge_token = "t" * 40
        try:
            bad = SimpleNamespace(headers={"Authorization": "Bearer пароль"})
            good = SimpleNamespace(headers={"Authorization": "Bearer " + "t" * 40})
            self.assertFalse(bot._hermes_bridge_authorized(bad))
            self.assertTrue(bot._hermes_bridge_authorized(good))
        finally:
            bot.settings.hermes_bridge_token = original

    def test_generated_transparent_png_is_flattened_on_white(self):
        buffer = io.BytesIO()
        Image.new("RGBA", (64, 64), (0, 0, 0, 0)).save(buffer, format="PNG")
        result = generation._normalise_generated_image(buffer.getvalue())
        with Image.open(io.BytesIO(result)) as image:
            self.assertEqual(image.convert("RGB").getpixel((10, 10)), (255, 255, 255))


class DbTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.root / 't.db'}")
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.original_session = bot.Session
        bot.Session = self.factory
        async with self.engine.begin() as connection:
            await connection.run_sync(bot.Base.metadata.create_all)

    async def asyncTearDown(self):
        bot.Session = self.original_session
        await self.engine.dispose()
        self.temp.cleanup()

    async def test_prefix_search_escapes_like_wildcards(self):
        async with self.factory() as db:
            client = bot.Client(telegram_id=5)
            db.add(client)
            await db.flush()
            db.add(bot.Order(id="abcdef12-0000", client_id=client.id))
            await db.commit()
        async with self.factory() as db:
            self.assertEqual(await bot.find_orders_by_prefix(db, "________"), [])
            self.assertEqual(await bot.find_orders_by_prefix(db, "%%%%%%%%"), [])
            self.assertEqual(await bot.find_orders_by_prefix(db, "abc"), [])  # слишком коротко
            found = await bot.find_orders_by_prefix(db, "abcdef12")
            self.assertEqual([o.id for o in found], ["abcdef12-0000"])

    async def test_fsm_rows_are_removed_after_clear(self):
        storage = bot.DatabaseStorage()
        key = StorageKey(bot_id=1, chat_id=2, user_id=3)
        await storage.set_state(key, "Brief:product")
        await storage.update_data(key, {"product": "x"})
        async with self.factory() as db:
            self.assertEqual(await db.scalar(bot.select(bot.func.count()).select_from(bot.FSMRecord)), 1)
        await storage.set_state(key, None)
        await storage.set_data(key, {})
        async with self.factory() as db:
            self.assertEqual(await db.scalar(bot.select(bot.func.count()).select_from(bot.FSMRecord)), 0)

    async def test_examples_use_input_file_not_raw_handle(self):
        folder = self.root / "examples"
        folder.mkdir()
        Image.new("RGB", (40, 40), "red").save(folder / "one.png")
        original = bot.EXAMPLES_DIR
        bot.EXAMPLES_DIR = folder
        try:
            message = SimpleNamespace(answer=AsyncMock(), answer_photo=AsyncMock())
            await bot.examples(message)
        finally:
            bot.EXAMPLES_DIR = original
        sent = message.answer_photo.await_args.args[0]
        self.assertIsInstance(sent, FSInputFile)

    async def test_long_order_card_is_split(self):
        async with self.factory() as db:
            client = bot.Client(telegram_id=77, first_name="Аня")
            db.add(client)
            await db.flush()
            db.add(bot.Order(
                id="longorder-0001", client_id=client.id, status="admin_review",
                brief={"product": "Чайник", "advantages": "а" * 2000, "required_text": "б" * 2000,
                       "reference_texts": ["в" * 2000 for _ in range(10)]},
            ))
            await db.commit()
        sent = []

        class Bot:
            async def send_message(self, chat_id, text, reply_markup=None):
                sent.append((text, reply_markup))

        self.assertTrue(await bot.send_order_card(Bot(), 1, "longorder-0001"))
        self.assertGreater(len(sent), 1)
        self.assertTrue(all(len(text) <= 4096 for text, _ in sent))
        self.assertIsNone(sent[0][1])
        self.assertIsNotNone(sent[-1][1])  # кнопки решения — на последней части


if __name__ == "__main__":
    unittest.main()
