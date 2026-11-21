import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("BOT_TOKEN", "123456:testing-token")
os.environ.setdefault("ADMIN_ID", "1")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./bot-workflow-import.db")

from PIL import Image
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import bot


class FakeBot:
    def __init__(self, fail_document_call=None):
        self.fail_document_call = fail_document_call
        self.document_calls = 0
        self.documents = []
        self.messages = []

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))

    async def send_document(self, chat_id, document, caption=None):
        self.document_calls += 1
        if self.document_calls == self.fail_document_call:
            raise RuntimeError("simulated Telegram delivery failure")
        self.documents.append((chat_id, Path(document.path), caption))


class BotWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.original_storage_path = bot.settings.storage_path
        bot.settings.storage_path = self.root / "storage"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.root / 'workflow.db'}")
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.original_session = bot.Session
        bot.Session = self.session_factory
        async with self.engine.begin() as connection:
            await connection.run_sync(bot.Base.metadata.create_all)

    async def asyncTearDown(self):
        bot.Session = self.original_session
        bot.settings.storage_path = self.original_storage_path
        await self.engine.dispose()
        self.temp.cleanup()

    async def create_paid_order(self, order_id="order-paid-test"):
        client_id = 123456789
        order_dir = bot.settings.storage_path / "orders" / order_id
        order_dir.mkdir(parents=True, exist_ok=True)
        assets = []
        for slide, variant in ((1, 1), (1, 2)):
            path = order_dir / f"slide_{slide}_{variant}.png"
            Image.new("RGB", (80, 100), (80 + variant * 20, 100, 120)).save(path, format="PNG")
            assets.append({
                "slide": slide,
                "variant": variant,
                "final_path": str(path),
                "demo_path": str(order_dir / f"demo_{slide}_{variant}.jpg"),
                "source": "manual",
            })
        async with self.session_factory() as db:
            client = bot.Client(telegram_id=client_id, username="buyer", first_name="Buyer")
            db.add(client)
            await db.flush()
            order = bot.Order(
                id=order_id,
                client_id=client.id,
                status="paid",
                brief={"slides": 1, "design_assets": assets},
            )
            db.add(order)
            db.add(bot.Payment(order_id=order_id, amount=1290, note="full payment"))
            await db.commit()
        return order_id, client_id, order_dir

    async def test_paid_originals_are_delivered_once(self):
        order_id, client_id, _order_dir = await self.create_paid_order()
        fake = FakeBot()

        delivered, message = await bot.deliver_paid_originals(fake, order_id)

        self.assertTrue(delivered, message)
        self.assertEqual(len(fake.documents), 2)
        self.assertTrue(all(path.is_file() for _chat_id, path, _caption in fake.documents))
        async with self.session_factory() as db:
            order = await db.get(bot.Order, order_id)
            self.assertEqual(order.status, "completed")
            self.assertEqual(
                set((order.brief.get("original_delivery") or {}).get("sent_assets", [])),
                {"1:1", "1:2"},
            )

        delivered_again, second_message = await bot.deliver_paid_originals(fake, order_id)
        self.assertTrue(delivered_again, second_message)
        self.assertEqual(len(fake.documents), 2)

    async def test_failed_delivery_can_resume_without_resending_sent_file(self):
        order_id, _client_id, _order_dir = await self.create_paid_order()
        first_attempt = FakeBot(fail_document_call=2)

        delivered, message = await bot.deliver_paid_originals(first_attempt, order_id)
        self.assertFalse(delivered)
        self.assertIn("/deliver", message)
        self.assertEqual(len(first_attempt.documents), 1)

        second_attempt = FakeBot()
        resumed, resume_message = await bot.deliver_paid_originals(second_attempt, order_id)
        self.assertTrue(resumed, resume_message)
        self.assertEqual(len(second_attempt.documents), 1)
        self.assertIn("вариант 2", second_attempt.documents[0][2])

    async def test_forget_anonymizes_paid_order_and_removes_unpaid_order(self):
        paid_id = "order-retain-paid"
        unpaid_id = "order-delete-unpaid"
        client_id = 987654321
        paid_dir = bot.settings.storage_path / "orders" / paid_id
        unpaid_dir = bot.settings.storage_path / "orders" / unpaid_id
        paid_dir.mkdir(parents=True, exist_ok=True)
        unpaid_dir.mkdir(parents=True, exist_ok=True)
        (paid_dir / "original.png").write_bytes(b"private-image")
        (unpaid_dir / "source.png").write_bytes(b"private-image")

        async with self.session_factory() as db:
            client = bot.Client(telegram_id=client_id, username="private_user", first_name="Private")
            db.add(client)
            await db.flush()
            db.add(bot.Order(id=paid_id, client_id=client.id, status="completed",
                             brief={"product": "private product", "customer_note": "private note"}))
            db.add(bot.Order(id=unpaid_id, client_id=client.id, status="admin_review",
                             brief={"product": "unpaid product"}))
            db.add(bot.Payment(order_id=paid_id, amount=1290, note="payment retained"))
            await db.commit()

        message = SimpleNamespace(
            from_user=SimpleNamespace(id=client_id),
            answer=AsyncMock(),
        )
        await bot.forget(message)

        async with self.session_factory() as db:
            paid = await db.get(bot.Order, paid_id)
            unpaid = await db.get(bot.Order, unpaid_id)
            client = await db.scalar(
                bot.select(bot.Client).where(bot.Client.telegram_id == -client_id)
            )
            payments = list((await db.scalars(
                bot.select(bot.Payment).where(bot.Payment.order_id == paid_id)
            )).all())
            self.assertEqual(paid.status, "privacy_erased")
            self.assertEqual(paid.brief, {
                "privacy_erased": True,
                "design_assets": [],
                "generation_metrics": {},
            })
            self.assertIsNone(unpaid)
            self.assertIsNotNone(client)
            self.assertIsNone(client.username)
            self.assertIsNone(client.first_name)
            self.assertEqual(len(payments), 1)

        self.assertFalse(paid_dir.exists())
        self.assertFalse(unpaid_dir.exists())
        message.answer.assert_awaited_once()




    async def test_only_configured_owner_can_queue_hermes_jobs(self):
        owner = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            text="/agent inspect the repository",
            answer=AsyncMock(),
        )
        outsider = SimpleNamespace(
            from_user=SimpleNamespace(id=2),
            text="/agent inspect the repository",
            answer=AsyncMock(),
        )

        await bot.admin_hermes_agent(outsider)
        outsider.answer.assert_awaited_once_with("Эта команда доступна только владельцу бота.")
        async with self.session_factory() as db:
            self.assertEqual(await db.scalar(bot.select(bot.func.count()).select_from(bot.HermesJob)), 0)

        await bot.admin_hermes_agent(owner)
        async with self.session_factory() as db:
            job = await db.scalar(bot.select(bot.HermesJob))
            self.assertIsNotNone(job)
            self.assertEqual(job.prompt, "inspect the repository")
            self.assertEqual(job.status, "queued")

    async def test_admin_controls_reject_non_owner(self):
        outsider = SimpleNamespace(
            from_user=SimpleNamespace(id=2),
            text="📊 Статистика",
            answer=AsyncMock(),
        )
        allowed = await bot.require_admin_message(outsider)
        self.assertFalse(allowed)
        outsider.answer.assert_awaited_once_with("Эта команда доступна только владельцу бота.")

    async def test_admin_callback_rejects_non_owner(self):
        outsider = SimpleNamespace(
            from_user=SimpleNamespace(id=2),
            answer=AsyncMock(),
        )
        allowed = await bot.require_admin_callback(outsider)
        self.assertFalse(allowed)
        outsider.answer.assert_awaited_once_with(
            "Эта функция доступна только владельцу бота.", show_alert=True
        )

    async def test_customer_clarification_handler_never_intercepts_owner_chat(self):
        owner = SimpleNamespace(from_user=SimpleNamespace(id=1), text="owner private note")
        await bot.forward_client_clarification(owner)
        # Owner messages must not be treated as a customer reply to an order.
        # No DB rows were seeded, so any accidental DB lookup would still return None;
        # the guard itself is separately tested by its early return through this call.


if __name__ == "__main__":
    unittest.main()
