import asyncio
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("BOT_TOKEN", "123456:abcdefghijklmnopqrstuvwxyzABCDE12345")
os.environ.setdefault("ADMIN_ID", "123")

from PIL import Image
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select
import bot
from image_generation import build_slide_prompts, generate_slide_image, GenerationError


def image_bytes():
    b = io.BytesIO()
    Image.new("RGB", (20, 30), "blue").save(b, format="PNG")
    return b.getvalue()


class FakeBot:
    def __init__(self):
        self.messages = []
        self.photos = []
    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text))
    async def send_photo(self, chat_id, photo, **kwargs):
        self.photos.append(chat_id)


class PipelineTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_session, self.old_path = bot.Session, bot.settings.storage_path
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        bot.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        bot.settings.storage_path = Path(self.tmp.name)
        async with self.engine.begin() as conn:
            await conn.run_sync(bot.Base.metadata.create_all)
        async with bot.Session() as db:
            client = bot.Client(telegram_id=456)
            db.add(client)
            await db.flush()
            order = bot.Order(client_id=client.id, status="accepted", source="bot", brief={"slides": 2, "product": "Чашка"})
            db.add(order)
            await db.commit()
            self.order_id = order.id

    async def asyncTearDown(self):
        bot.Session, bot.settings.storage_path = self.old_session, self.old_path
        await self.engine.dispose()
        self.tmp.cleanup()

    async def test_generate_delivers_to_both_without_approval(self):
        async def fake_generate(**kwargs):
            return image_bytes()
        fake = FakeBot()
        with patch.object(bot, "generate_slide_image", fake_generate):
            self.assertTrue(await bot.auto_generate_for_order(fake, self.order_id))
        self.assertEqual(fake.photos.count(123), 2)
        self.assertEqual(fake.photos.count(456), 2)
        async with bot.Session() as db:
            order = await db.get(bot.Order, self.order_id)
            self.assertEqual(order.status, "demo_sent")
            self.assertEqual(len(order.brief["design_assets"]), 2)

    async def test_incomplete_generation_never_delivers(self):
        calls = 0
        async def fake_generate(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise GenerationError("failed")
            return image_bytes()
        fake = FakeBot()
        with patch.object(bot, "generate_slide_image", fake_generate):
            self.assertFalse(await bot.auto_generate_for_order(fake, self.order_id))
        self.assertNotIn(456, fake.photos)
        async with bot.Session() as db:
            order = await db.get(bot.Order, self.order_id)
            self.assertEqual(order.status, "generation_failed")

    async def test_claim_prevents_duplicate_generation(self):
        count = 0
        async def fake_generate(**kwargs):
            nonlocal count
            count += 1
            await asyncio.sleep(0.01)
            return image_bytes()
        fake = FakeBot()
        with patch.object(bot, "generate_slide_image", fake_generate):
            results = await asyncio.gather(bot.auto_generate_for_order(fake, self.order_id), bot.auto_generate_for_order(fake, self.order_id))
        self.assertEqual(count, 2)
        self.assertEqual(sum(results), 1)

    async def test_missing_key_fails_closed(self):
        with self.assertRaises(GenerationError):
            await generate_slide_image("test", "WB", openai_api_key=None)

    def test_prompts_include_revisions_and_all_slides(self):
        prompts = build_slide_prompts({"slides": 12, "product": "чашка", "style": "Правки клиента: красный фон"})
        self.assertEqual(len(prompts), 12)
        self.assertIn("красный фон", prompts[0])

if __name__ == "__main__":
    unittest.main()
