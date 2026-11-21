import asyncio
import enum
import os
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaDocument, InputMediaPhoto, KeyboardButton, Message,
    ReplyKeyboardMarkup, ReplyKeyboardRemove, FSInputFile,
)
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, JSON, String, Text, delete, select, update, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from generation import ImageGenerationError, build_product_prompt, generate_two_images
from image_pipeline import ImagePipelineError, apply_text_overlay, make_watermarked_demo


class Settings(BaseSettings):
    bot_token: str
    admin_id: int
    database_url: str = "sqlite+aiosqlite:///./card_studio.db"
    storage_path: Path = Path("./data")
    max_file_mb: int = 15
    max_files_per_order: int = 12
    demo_long_edge: int = 700
    demo_watermark_text: str = "ПЕРВЫЙ СЛАЙД • ДЕМО"
    image_mode: str = "auto"
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_image_model: str = "gpt-image-2"
    openai_image_size: str = "1024x1536"
    openai_image_quality: str = "medium"
    generation_timeout_seconds: int = 180
    max_generation_slides: int = 10
    max_order_cost_usd: float = 5.0
    estimated_image_cost_usd: float = 0.25
    text_overlay_enabled: bool = False
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def db_url(self):
        url = self.database_url
        if url.startswith("postgres://"):
            return url.replace("postgres://", "postgresql+asyncpg://", 1)
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url


settings = Settings()
settings.storage_path.mkdir(parents=True, exist_ok=True)
engine = create_async_engine(settings.db_url, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


def now():
    return datetime.now(timezone.utc)


class Client(Base):
    __tablename__ = "clients"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    stopped: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    status: Mapped[str] = mapped_column(String(40), default="lead")
    source: Mapped[str] = mapped_column(String(24), default="bot")
    brief: Mapped[dict] = mapped_column(JSON, default=dict)
    assistant_notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class OrderFile(Base):
    __tablename__ = "order_files"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    telegram_file_id: Mapped[str] = mapped_column(String(256))
    file_type: Mapped[str] = mapped_column(String(24))
    original_name: Mapped[str | None] = mapped_column(String(255))


class FSMRecord(Base):
    __tablename__ = "fsm_records"
    key: Mapped[str] = mapped_column(String(512), primary_key=True)
    state: Mapped[str | None] = mapped_column(String(255))
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class AssistantMaterial(Base):
    __tablename__ = "assistant_materials"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admin_chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    admin_message_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    source_user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    source_username: Mapped[str | None] = mapped_column(String(64))
    text_content: Mapped[str | None] = mapped_column(Text)
    telegram_file_id: Mapped[str | None] = mapped_column(String(256))
    file_type: Mapped[str | None] = mapped_column(String(24))
    original_name: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DatabaseStorage(BaseStorage):
    def _key(self, key: StorageKey):
        return ":".join(str(x or "") for x in (
            key.bot_id, key.chat_id, key.user_id, getattr(key, "thread_id", None),
            getattr(key, "business_connection_id", None), getattr(key, "destiny", "default")
        ))

    async def close(self):
        return None

    async def get_state(self, key: StorageKey):
        async with Session() as db:
            row = await db.get(FSMRecord, self._key(key))
            return row.state if row else None

    async def set_state(self, key: StorageKey, state: Any = None):
        value = state.state if hasattr(state, "state") else state
        async with Session() as db:
            row = await db.get(FSMRecord, self._key(key))
            if row is None:
                if value is not None:
                    db.add(FSMRecord(key=self._key(key), state=str(value), data={}))
            else:
                row.state = str(value) if value is not None else None
            await db.commit()

    async def get_data(self, key: StorageKey):
        async with Session() as db:
            row = await db.get(FSMRecord, self._key(key))
            return dict(row.data or {}) if row else {}

    async def set_data(self, key: StorageKey, data):
        async with Session() as db:
            row = await db.get(FSMRecord, self._key(key))
            if row is None:
                db.add(FSMRecord(key=self._key(key), state=None, data=dict(data)))
            else:
                row.data = dict(data)
            await db.commit()

    async def update_data(self, key: StorageKey, data):
        merged = await self.get_data(key)
        merged.update(data)
        await self.set_data(key, merged)
        return merged


class Brief(StatesGroup):
    platform = State()
    product = State()
    advantages = State()
    required_text = State()
    style = State()
    colors = State()
    slides = State()
    deadline = State()
    materials = State()
    confirmation = State()


class Contact(StatesGroup):
    message = State()



class AdminAction(StatesGroup):
    clarification = State()
    manual_images = State()
    lookup_order = State()
    set_price = State()
    broadcast = State()


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    amount: Mapped[int] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


router = Router()


def menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Заказать карточку"), KeyboardButton(text="Примеры работ")],
        [KeyboardButton(text="Цены"), KeyboardButton(text="Связаться с автором")],
    ], resize_keyboard=True)


def admin_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📥 Все заявки"), KeyboardButton(text="🕒 На проверке")],
        [KeyboardButton(text="🎨 На генерации"), KeyboardButton(text="👁 На согласовании")],
        [KeyboardButton(text="💰 Оплаты"), KeyboardButton(text="👥 Клиенты")],
        [KeyboardButton(text="🆔 Найти по ID"), KeyboardButton(text="⚙️ Настройки")],
        [KeyboardButton(text="📣 Рассылка"), KeyboardButton(text="📊 Статистика")],
        [KeyboardButton(text="⚙️ Состояние бота"), KeyboardButton(text="👤 Режим клиента")],
    ], resize_keyboard=True)


def admin_only(user_id: int) -> bool:
    return user_id == settings.admin_id


def platform_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="WB"), KeyboardButton(text="Ozon"), KeyboardButton(text="Avito")],
        [KeyboardButton(text="Отмена")],
    ], resize_keyboard=True, one_time_keyboard=True)


def confirm_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подтвердить бриф", callback_data="brief:confirm")],
        [InlineKeyboardButton(text="Заполнить заново", callback_data="brief:restart")],
    ])


def order_action_menu(order_id: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Принять заявку", callback_data=f"order:accept:{order_id}")],
        [InlineKeyboardButton(text="Запросить уточнение", callback_data=f"order:clarify:{order_id}")],
        [InlineKeyboardButton(text="Отклонить заявку", callback_data=f"order:reject:{order_id}")],
    ])


def generate_order_menu(order_id: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Принять и создать", callback_data=f"design:create:{order_id}")]
    ])


async def send_order_card(bot: Bot, admin_chat_id: int, order_id: str):
    async with Session() as db:
        order = await db.get(Order, order_id)
        if order is None:
            return False
        client = await db.get(Client, order.client_id)
        attachments = list((await db.scalars(
            select(OrderFile).where(OrderFile.order_id == order_id).order_by(OrderFile.id)
        )).all())
        brief = dict(order.brief or {})
        client_id = client.telegram_id if client else 0
        username = client.username if client else None
        first_name = client.first_name if client else None
        status = order.status
        source = order.source
        notes = order.assistant_notes
        created_at = order.created_at
        payments = list((await db.scalars(
            select(Payment).where(Payment.order_id == order_id).order_by(Payment.created_at.desc())
        )).all())

    status_labels = {
        "lead": "Лид", "brief": "Черновик", "admin_review": "На проверке",
        "waiting_client_info": "Ждём клиента", "accepted": "Принята",
        "generating": "Генерация", "manual_pending": "Ручная загрузка",
        "demo_review": "Демо на проверке", "sending_demo": "Отправка демо",
        "demo_sent": "Демо отправлено", "paid": "Оплачена",
        "completed": "Завершена", "rejected": "Отклонена", "cancelled": "Отменена",
    }
    status_label = status_labels.get(status, status)
    created_label = created_at.strftime("%d.%m.%Y %H:%M UTC") if created_at else "дата неизвестна"
    who = first_name or "Имя не указано"
    if username:
        who += f" (@{username})"
    product = str(brief.get("product") or notes or "Товар не указан")
    product = " ".join(product.split())[:110]
    platform = brief.get("platform") or "Площадка не указана"
    slides = brief.get("slides")
    slide_label = f"{slides} слайд(ов)" if slides else "количество слайдов не указано"

    # First block matches the compact cards in the order list.
    lines = [
        f"№{order_id[:8]} · {status_label}",
        f"Клиент: {who}",
        f"Товар: {product}",
        f"Площадка: {platform} · {slide_label}",
        f"Материалов: {len(attachments)} · {created_label}",
        "",
        "ПОДРОБНОСТИ ЗАЯВКИ",
        f"Полный номер: {order_id}",
        f"Telegram ID: {client_id}",
        f"Источник: {'анкета бота' if source == 'bot' else source}",
    ]
    for key, label in (
        ("advantages", "Преимущества"),
        ("required_text", "Обязательные тексты и характеристики"),
        ("style", "Стиль"),
        ("colors", "Цвета"),
        ("deadline", "Срок"),
    ):
        value = brief.get(key)
        if value not in (None, ""):
            if isinstance(value, (list, tuple)):
                value = ", ".join(str(item) for item in value)
            lines.append(f"{label}: {value}")
    reference_texts = brief.get("reference_texts") or []
    if reference_texts:
        lines.append("Референсы: " + " | ".join(str(x) for x in reference_texts))
    if notes and notes != product:
        lines.append(f"Дополнительная информация: {notes}")
    if payments:
        total_paid = sum(int(p.amount or 0) for p in payments)
        lines.append(f"Оплаты: {total_paid} RUB ({len(payments)} записей)")
        for payment in payments:
            paid_at = payment.created_at.strftime("%d.%m.%Y %H:%M UTC") if payment.created_at else "дата неизвестна"
            suffix = f" · {payment.note}" if payment.note else ""
            lines.append(f"  • {payment.amount} RUB · {paid_at}{suffix}")
    else:
        lines.append("Оплаты: не зарегистрированы")

    if status == "accepted":
        keyboard = generate_order_menu(order_id)
    elif status == "manual_pending":
        keyboard = manual_generation_menu(order_id)
    elif status == "demo_review":
        keyboard = design_review_menu(order_id)
    elif status == "admin_review":
        keyboard = order_action_menu(order_id)
    else:
        keyboard = None
    await bot.send_message(admin_chat_id, "\n".join(lines), reply_markup=keyboard)
    for index, item in enumerate(attachments, start=1):
        try:
            caption = f"Материал {index}/{len(attachments)} · заявка {order_id[:8]}"
            if item.file_type == "photo":
                await bot.send_photo(admin_chat_id, item.telegram_file_id, caption=caption)
            else:
                await bot.send_document(admin_chat_id, item.telegram_file_id, caption=caption)
        except Exception:
            await bot.send_message(admin_chat_id, f"Не удалось повторно отправить материал {index} заявки {order_id[:8]}.")
    return True

async def get_client(db, user):
    client = await db.scalar(select(Client).where(Client.telegram_id == user.id))
    if client is None:
        client = Client(telegram_id=user.id, username=user.username, first_name=user.first_name)
        db.add(client)
        await db.flush()
    else:
        client.username, client.first_name = user.username, user.first_name
    return client


@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    async with Session() as db:
        client = await get_client(db, message.from_user)
        client.stopped = False
        await db.commit()
    welcome = (
        "Здравствуйте! Я помощник студии «Первый слайд». "
        "Помогу оформить заявку на создание карточки товара. "
        "На ключевых этапах работу проверяет администратор."
    )
    if admin_only(message.from_user.id):
        welcome += "\n\nПанель администратора. Управляй заявками кнопками ниже."
        await message.answer(welcome, reply_markup=admin_menu())
    else:
        await message.answer(welcome, reply_markup=menu())



@router.message(F.text == "📥 Все заявки")
async def admin_all_orders(message: Message):
    if not admin_only(message.from_user.id):
        return
    await orders(message)


@router.message(F.text == "🕒 На проверке")
async def admin_pending_orders(message: Message):
    if not admin_only(message.from_user.id):
        return
    await send_orders_list(message, statuses=["admin_review"], limit=30)


@router.message(F.text == "⚙️ Состояние бота")
async def admin_health_button(message: Message):
    if not admin_only(message.from_user.id):
        return
    await health(message)


@router.message(F.text.in_({"🆔 Найти по ID", "🔎 Открыть заявку"}))
async def admin_review_button(message: Message, state: FSMContext):
    if not admin_only(message.from_user.id):
        return
    await state.clear()
    await state.set_state(AdminAction.lookup_order)
    await message.answer("Введи ID заявки минимум из 8 символов. Кнопки в карточках заявок открывают их напрямую. Для отмены нажми «Отмена».")


@router.message(AdminAction.lookup_order, F.text.in_({
    "📥 Все заявки", "🕒 На проверке", "🎨 На генерации", "👁 На согласовании",
    "💰 Оплаты", "👥 Клиенты", "⚙️ Настройки", "📣 Рассылка", "📊 Статистика",
    "⚙️ Состояние бота", "👤 Режим клиента", "🆔 Найти по ID", "🔎 Открыть заявку",
}))
async def admin_lookup_navigation(message: Message, state: FSMContext):
    if not admin_only(message.from_user.id):
        return
    value = (message.text or "").strip()
    await state.clear()
    if value == "📥 Все заявки":
        await orders(message)
    elif value == "🕒 На проверке":
        await admin_pending_orders(message)
    elif value == "🎨 На генерации":
        await admin_generating_orders(message)
    elif value == "👁 На согласовании":
        await admin_reviewing_orders(message)
    elif value == "💰 Оплаты":
        await admin_payment_menu(message)
    elif value == "👥 Клиенты":
        await admin_clients(message)
    elif value == "⚙️ Настройки":
        await admin_settings(message)
    elif value == "📣 Рассылка":
        await admin_broadcast_start(message, state)
    elif value == "📊 Статистика":
        await admin_stats(message)
    elif value == "⚙️ Состояние бота":
        await health(message)
    elif value == "👤 Режим клиента":
        await admin_client_mode(message)
    elif value in {"🆔 Найти по ID", "🔎 Открыть заявку"}:
        await state.set_state(AdminAction.lookup_order)
        await message.answer("Введи ID заявки минимум из 8 символов. Для отмены нажми «Отмена».")


@router.message(AdminAction.lookup_order, F.text)
async def admin_lookup_order(message: Message, state: FSMContext):
    if not admin_only(message.from_user.id):
        return
    value = (message.text or "").strip()
    if value.casefold() in {"отмена", "/cancel"}:
        await state.clear()
        await message.answer("Поиск отменён.", reply_markup=admin_menu())
        return
    if len(value) < 8:
        await message.answer("ID слишком короткий. Введи минимум 8 символов или нажми «Отмена».")
        return
    await state.clear()
    await review_order(message, f"/review {value}")


@router.message(F.text == "👤 Режим клиента")
async def admin_client_mode(message: Message):
    if not admin_only(message.from_user.id):
        return
    await message.answer("Клиентское меню включено. Для возврата к панели администратора напиши /start.", reply_markup=menu())


@router.message(F.text == "💳 Оплаты")
async def admin_payments_button(message: Message):
    if not admin_only(message.from_user.id):
        return
    await message.answer("Подтверждение оплат пока не реализовано. Оригиналы не выдаются без отдельного подтверждения.", reply_markup=admin_menu())


@router.message(F.text == "📨 Напоминания")
async def admin_reminders_button(message: Message):
    if not admin_only(message.from_user.id):
        return
    await message.answer("Отправка напоминаний пока не реализована.", reply_markup=admin_menu())



@router.message(F.text == "🎨 На генерации")
async def admin_generating_orders(message: Message):
    if not admin_only(message.from_user.id): return
    await admin_list_status(message, ["accepted", "manual_pending", "generating"])


@router.message(F.text == "👁 На согласовании")
async def admin_reviewing_orders(message: Message):
    if not admin_only(message.from_user.id): return
    await admin_list_status(message, ["demo_review", "demo_sent"])


async def admin_list_status(message: Message, statuses: list[str]):
    await send_orders_list(message, statuses=statuses, limit=30)


@router.message(F.text == "💰 Оплаты")
async def admin_payment_menu(message: Message):
    if not admin_only(message.from_user.id): return
    async with Session() as db:
        rows = list((await db.scalars(
            select(Order).where(Order.status.in_(["paid", "completed", "demo_sent"]))
            .order_by(Order.created_at.desc()).limit(20)
        )).all())
        payments = list((await db.scalars(select(Payment).order_by(Payment.created_at.desc()).limit(20))).all())
    text = ["Последние подтверждения оплат:"]
    if payments:
        text += [f"{p.order_id[:8]} · {p.amount} RUB · {p.note or 'без комментария'}" for p in payments]
    else:
        text.append("Подтверждений пока нет.")
    text.append("\nДля регистрации оплаты используй /paid ID СУММА [комментарий].")
    await message.answer("\n".join(text), reply_markup=admin_menu())


@router.message(F.text == "👥 Клиенты")
async def admin_clients(message: Message):
    if not admin_only(message.from_user.id): return
    async with Session() as db:
        clients = list((await db.scalars(select(Client).order_by(Client.created_at.desc()).limit(30))).all())
        total = await db.scalar(select(func.count(Client.id)))
    if not clients:
        await message.answer("Клиентов пока нет.", reply_markup=admin_menu())
        return
    lines = [f"Клиентов в базе: {total}"]
    lines += [f"{x.first_name or 'Без имени'} · @{x.username or 'нет username'} · ID {x.telegram_id}" for x in clients]
    await message.answer("\n".join(lines), reply_markup=admin_menu())


@router.message(F.text == "⚙️ Настройки")
async def admin_settings(message: Message):
    if not admin_only(message.from_user.id): return
    await message.answer(
        f"Режим генерации: {settings.image_mode}\nМодель: {settings.openai_image_model}\n"
        f"Лимит слайдов: {settings.max_generation_slides}\nЛимит резерва на заявку: USD {settings.max_order_cost_usd:.2f}\n"
        "Прайс хранится в config/price.yaml. Для изменения используй /price.",
        reply_markup=admin_menu(),
    )


@router.message(F.text == "📊 Статистика")
async def admin_stats(message: Message):
    if not admin_only(message.from_user.id): return
    async with Session() as db:
        total_orders = await db.scalar(select(func.count(Order.id)))
        total_clients = await db.scalar(select(func.count(Client.id)))
        statuses = list((await db.execute(select(Order.status, func.count(Order.id)).group_by(Order.status))).all())
    lines = [f"Клиентов: {total_clients or 0}", f"Заявок: {total_orders or 0}", "По статусам:"]
    lines += [f"• {status}: {count}" for status, count in statuses]
    await message.answer("\n".join(lines), reply_markup=admin_menu())


@router.message(F.text == "📣 Рассылка")
async def admin_broadcast_start(message: Message, state: FSMContext):
    if not admin_only(message.from_user.id): return
    await state.clear()
    await state.set_state(AdminAction.broadcast)
    await message.answer("Пришли текст рассылки. Для отмены напиши «Отмена». Сообщение отправится активным клиентам из базы.")


@router.message(AdminAction.broadcast, F.text)
async def admin_broadcast_send(message: Message, state: FSMContext):
    if not admin_only(message.from_user.id): return
    body = (message.text or "").strip()
    if body.casefold() == "отмена":
        await state.clear()
        await message.answer("Рассылка отменена.", reply_markup=admin_menu())
        return
    await state.clear()
    async with Session() as db:
        ids = list((await db.scalars(select(Client.telegram_id).where(Client.stopped.is_(False)))).all())
    sent = failed = 0
    for telegram_id in ids:
        try:
            await message.bot.send_message(telegram_id, body)
            sent += 1
        except Exception:
            failed += 1
    await message.answer(f"Рассылка завершена. Успешно: {sent}; ошибок: {failed}.", reply_markup=admin_menu())


@router.message(Command("price"))
async def admin_price_command(message: Message, state: FSMContext):
    if not admin_only(message.from_user.id): return
    await state.clear()
    await state.set_state(AdminAction.set_price)
    await message.answer("Отправь новый прайс в формате: Название — сумма, по одной позиции на строку. Для отмены: Отмена.")


@router.message(AdminAction.set_price, F.text)
async def admin_price_save(message: Message, state: FSMContext):
    if not admin_only(message.from_user.id): return
    if (message.text or "").strip().casefold() == "отмена":
        await state.clear()
        await message.answer("Изменение прайса отменено.", reply_markup=admin_menu())
        return
    items = []
    for line in (message.text or "").splitlines():
        if not line.strip(): continue
        parts = re.split(r"\s*[—–-]\s*", line.strip(), maxsplit=1)
        if len(parts) != 2:
            await message.answer("Не понял строку. Используй формат «Название — сумма».")
            return
        try: amount = int(parts[1].replace(" ", "").replace("₽", "").replace("руб.", ""))
        except ValueError:
            await message.answer(f"Некорректная сумма: {parts[1]}")
            return
        if amount < 0:
            await message.answer("Сумма не может быть отрицательной.")
            return
        items.append({"name": parts[0][:100], "price": amount})
    if not items:
        await message.answer("Прайс пуст. Отправь хотя бы одну позицию.")
        return
    path = Path("config/price.yaml")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"currency": "RUB", "items": items}, allow_unicode=True, sort_keys=False), encoding="utf-8")
    await state.clear()
    await message.answer(f"Прайс обновлён: {len(items)} позиций.", reply_markup=admin_menu())


@router.message(F.text == "Заказать карточку")
async def begin(message: Message, state: FSMContext):
    if admin_only(message.from_user.id):
        await message.answer("Ты находишься в режиме администратора. Для оформления тестового заказа нажми «👤 Режим клиента».")
        return
    await state.clear()
    await state.set_state(Brief.platform)
    await message.answer("Для какой площадки нужна карточка?", reply_markup=platform_menu())


@router.message(Brief.platform)
async def platform_step(message: Message, state: FSMContext):
    if message.text == "Отмена":
        await state.clear()
        await message.answer("Заявка отменена.", reply_markup=menu())
        return
    if message.text not in {"WB", "Ozon", "Avito"}:
        await message.answer("Выбери WB, Ozon или Avito.")
        return
    await state.update_data(platform=message.text)
    await state.set_state(Brief.product)
    await message.answer(
        "Что за товар? Укажи название, модель и разновидность.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(Brief.product)
async def product_step(message: Message, state: FSMContext):
    await state.update_data(product=(message.text or "")[:1000])
    await state.set_state(Brief.advantages)
    await message.answer("Назови 3–5 главных преимуществ товара.")


@router.message(Brief.advantages)
async def advantages_step(message: Message, state: FSMContext):
    await state.update_data(advantages=(message.text or "")[:2000])
    await state.set_state(Brief.required_text)
    await message.answer("Какие тексты или характеристики обязательно разместить? Если нет — напиши «на усмотрение студии».")


@router.message(Brief.required_text)
async def text_step(message: Message, state: FSMContext):
    await state.update_data(required_text=(message.text or "")[:2000])
    await state.set_state(Brief.style)
    await message.answer("Какой стиль нужен? Например: минимализм, премиальный, яркий рекламный, технологичный.")


@router.message(Brief.style)
async def style_step(message: Message, state: FSMContext):
    await state.update_data(style=(message.text or "")[:1000])
    await state.set_state(Brief.colors)
    await message.answer("Какие цвета использовать или избегать?")


@router.message(Brief.colors)
async def colors_step(message: Message, state: FSMContext):
    await state.update_data(colors=(message.text or "")[:1000])
    await state.set_state(Brief.slides)
    await message.answer("Сколько слайдов требуется? Укажи число от 1 до 20.")


@router.message(Brief.slides)
async def slides_step(message: Message, state: FSMContext):
    try:
        count = int(message.text)
        if not 1 <= count <= 20:
            raise ValueError
    except (ValueError, TypeError):
        await message.answer("Укажи целое число от 1 до 20.")
        return
    await state.update_data(slides=count)
    await state.set_state(Brief.deadline)
    await message.answer("К какому сроку нужна работа? Напиши дату или «не срочно». Срок подтвердит администратор.")


@router.message(Brief.deadline)
async def deadline_step(message: Message, state: FSMContext):
    await state.update_data(deadline=(message.text or "")[:500], files=[], reference_texts=[])
    await state.set_state(Brief.materials)
    await message.answer("Сначала отправь главное фото товара, затем референсы. Можно несколько сообщений. Когда закончишь, напиши «Готово».")


@router.message(Brief.materials, F.text.casefold() == "готово")
async def brief_done(message: Message, state: FSMContext):
    data = await state.get_data()
    summary = (
        f"Площадка: {data.get('platform')}\nТовар: {data.get('product')}\n"
        f"Преимущества: {data.get('advantages')}\nТексты: {data.get('required_text')}\n"
        f"Стиль: {data.get('style')}\nЦвета: {data.get('colors')}\n"
        f"Слайдов: {data.get('slides')}\nСрок: {data.get('deadline')}\n"
        f"Файлов: {len(data.get('files', []))}\nРеференсов: {len(data.get('reference_texts', []))}"
    )
    await state.update_data(summary=summary)
    await state.set_state(Brief.confirmation)
    await message.answer("Проверь бриф:\n\n" + summary, reply_markup=confirm_menu())


@router.message(Brief.materials)
async def material_step(message: Message, state: FSMContext):
    file_id = kind = filename = None
    if message.photo:
        file_id, kind = message.photo[-1].file_id, "photo"
    elif message.document:
        doc = message.document
        if doc.file_size and doc.file_size > settings.max_file_mb * 1024 * 1024:
            await message.answer(f"Файл больше лимита {settings.max_file_mb} МБ.")
            return
        if doc.mime_type not in {"image/jpeg", "image/png", "image/webp"}:
            await message.answer("Пока принимаю изображения JPG, PNG и WEBP.")
            return
        file_id, kind, filename = doc.file_id, "document", doc.file_name
    elif message.text:
        data = await state.get_data()
        refs = list(data.get("reference_texts", []))
        refs.append(message.text[:2000])
        await state.update_data(reference_texts=refs[:20])
        await message.answer("Ссылка или описание референса сохранены. Пришли ещё или напиши «Готово».")
        return
    else:
        await message.answer("Пришли изображение, ссылку или напиши «Готово».")
        return
    data = await state.get_data()
    files = list(data.get("files", []))
    if len(files) >= settings.max_files_per_order:
        await message.answer(f"Достигнут лимит: {settings.max_files_per_order} файлов.")
        return
    files.append({"file_id": file_id, "type": kind, "name": filename})
    await state.update_data(files=files)
    await message.answer(f"Материал принят ({len(files)}). Можешь отправить следующий или написать «Готово».")


@router.callback_query(F.data == "brief:confirm", Brief.confirmation)
async def confirm_brief(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    async with Session() as db:
        client = await get_client(db, call.from_user)
        order = Order(client_id=client.id, status="admin_review", source="bot",
                      brief={k: v for k, v in data.items() if k not in {"files", "summary"}})
        db.add(order)
        await db.flush()
        for item in data.get("files", []):
            db.add(OrderFile(order_id=order.id, telegram_file_id=item["file_id"],
                             file_type=item["type"], original_name=item.get("name")))
        await db.commit()
        order_id = order.id
    await state.clear()
    await call.message.edit_text(f"Бриф принят. Номер заявки: {order_id[:8]}. Администратор проверит материалы и свяжется с тобой.")
    await call.answer()
    await send_order_card(call.bot, settings.admin_id, order_id)




@router.callback_query(F.data.startswith("order:"))
async def process_order_action(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != settings.admin_id:
        await call.answer("Это действие доступно только администратору.", show_alert=True)
        return

    try:
        _, action, order_id = call.data.split(":", 2)
    except (AttributeError, ValueError):
        await call.answer("Некорректная команда.", show_alert=True)
        return

    async with Session() as db:
        order = await db.get(Order, order_id)
        if order is None:
            await call.answer("Заявка не найдена.", show_alert=True)
            return
        if order.status != "admin_review":
            await call.answer(f"Заявка уже обработана. Статус: {order.status}", show_alert=True)
            return
        client = await db.get(Client, order.client_id)
        client_telegram_id = client.telegram_id if client else None

        if action == "accept":
            order.status = "accepted"
            await db.commit()
        elif action == "reject":
            order.status = "rejected"
            await db.commit()
        elif action == "clarify":
            await state.clear()
            await state.update_data(order_id=order_id)
            await state.set_state(AdminAction.clarification)
        else:
            await call.answer("Неизвестное действие.", show_alert=True)
            return

    if action == "clarify":
        await call.message.edit_reply_markup(reply_markup=None)
        await call.message.answer(
            f"Заявка {order_id[:8]}. Напиши одним сообщением, что нужно уточнить у клиента."
        )
        await call.answer("Жду текст уточнения.")
        return

    await call.message.edit_reply_markup(reply_markup=None)
    if action == "accept":
        client_message = (
            f"Заявка №{order_id[:8]} принята администратором. "
            "Сейчас проверяются материалы и условия заказа. "
            "Стоимость и сроки будут отдельно согласованы. Пока ничего оплачивать не нужно."
        )
        await call.answer("Заявка принята.")
        await call.message.answer(
            f"Заявка {order_id[:8]} принята. Нажми «Принять и создать», чтобы запустить автоматическую "
            "генерацию или получить пакет для ручной обработки.",
            reply_markup=generate_order_menu(order_id),
        )
    else:
        client_message = (
            f"К сожалению, студия пока не может принять заявку №{order_id[:8]}. "
            "По дополнительным вопросам можно написать через кнопку «Связаться с автором»."
        )
        await call.answer("Заявка отклонена.")

    if client_telegram_id:
        try:
            await call.bot.send_message(client_telegram_id, client_message)
        except Exception:
            await call.message.answer(
                "Статус обновлён, но сообщение клиенту не доставлено. "
                "Возможно, клиент ещё не открывал диалог с ботом."
            )


@router.message(AdminAction.clarification)
async def send_clarification_to_client(message: Message, state: FSMContext):
    if message.from_user.id != settings.admin_id:
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Нужно отправить текстовое сообщение. Напиши уточнение одним сообщением.")
        return

    data = await state.get_data()
    order_id = data.get("order_id")
    if not order_id:
        await state.clear()
        await message.answer("Не найден номер заявки. Открой заявку командой /review и повтори действие.")
        return

    async with Session() as db:
        order = await db.get(Order, order_id)
        if order is None or order.status != "admin_review":
            await state.clear()
            await message.answer("Заявка уже изменила статус. Обнови список через /orders.")
            return
        client = await db.get(Client, order.client_id)
        client_telegram_id = client.telegram_id if client else None
        order.status = "waiting_client_info"
        previous = order.assistant_notes or ""
        order.assistant_notes = (previous + "\n" if previous else "") + f"Уточнение от студии: {text}"
        await db.commit()

    await state.clear()
    if client_telegram_id:
        try:
            await message.bot.send_message(
                client_telegram_id,
                f"По заявке №{order_id[:8]} нужно уточнение:\n\n{text}\n\n"
                "Ответь следующим сообщением в этом чате — я передам ответ администратору."
            )
            await message.answer("Уточнение отправлено клиенту. Жду его ответ.")
        except Exception:
            await message.answer("Статус заявки сохранён, но Telegram не доставил сообщение клиенту.")
    else:
        await message.answer("У заявки не найден Telegram ID клиента.")


@router.callback_query(F.data == "brief:restart")
async def restart_brief(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(Brief.platform)
    await call.message.answer("Начнём бриф заново. Для какой площадки нужна карточка?", reply_markup=platform_menu())
    await call.answer()


@router.message(F.text == "Примеры работ")
async def examples(message: Message):
    folder = Path("content/examples")
    files = sorted(p for p in folder.glob("*") if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}) if folder.exists() else []
    if not files:
        await message.answer("Примеры пока не добавлены.")
        return
    for file in files[:10]:
        await message.answer_photo(file.open("rb"), caption=file.name)


@router.message(F.text == "Цены")
async def prices(message: Message):
    try:
        data = yaml.safe_load(Path("config/price.yaml").read_text(encoding="utf-8")) or {}
        items = data.get("items", [])
        currency = data.get("currency", "RUB")
        lines = [f"• {x['name']}: {x['price']} {currency}" for x in items if isinstance(x, dict) and x.get("name") and x.get("price") not in (None, "")]
    except Exception:
        lines = []
    await message.answer("\n".join(lines) if lines else "Прайс ещё не заполнен. Уточни стоимость у администратора.")


@router.message(F.text == "Связаться с автором")
async def contact_start(message: Message, state: FSMContext):
    await state.set_state(Contact.message)
    await state.update_data(contact_messages=[])
    await message.answer("Напиши сообщение. Можно несколькими сообщениями. Когда закончишь, напиши «Готово».")


@router.message(Contact.message, F.text.casefold() == "готово")
async def contact_done(message: Message, state: FSMContext):
    data = await state.get_data()
    body = "\n".join(data.get("contact_messages", []))
    if body:
        await message.bot.send_message(settings.admin_id, f"Обращение от {message.from_user.id} (@{message.from_user.username or 'нет username'}):\n\n{body}")
        await message.answer("Сообщение передано администратору.", reply_markup=menu())
    else:
        await message.answer("Пустое обращение отменено.", reply_markup=menu())
    await state.clear()


@router.message(Contact.message)
async def contact_collect(message: Message, state: FSMContext):
    data = await state.get_data()
    items = list(data.get("contact_messages", []))
    items.append((message.text or message.caption or "[вложение]")[:3000])
    await state.update_data(contact_messages=items[:10])
    await message.answer("Записал. Добавь ещё или напиши «Готово».")


@router.message(Command("health"))
async def health(message: Message):
    if message.from_user.id != settings.admin_id:
        return
    try:
        async with Session() as db:
            await db.execute(select(1))
        await message.answer("Бот отвечает. PostgreSQL доступен.")
    except Exception:
        await message.answer("Бот отвечает, но база данных недоступна.")


async def send_orders_list(message: Message, statuses: list[str] | None = None, limit: int = 20):
    async with Session() as db:
        query = (
            select(Order, Client)
            .join(Client, Client.id == Order.client_id)
            .order_by(Order.created_at.desc())
            .limit(limit)
        )
        if statuses:
            query = query.where(Order.status.in_(statuses))
        records = list((await db.execute(query)).all())
        order_ids = [order.id for order, _client in records]
        file_counts = {}
        if order_ids:
            counts = await db.execute(
                select(OrderFile.order_id, func.count(OrderFile.id))
                .where(OrderFile.order_id.in_(order_ids))
                .group_by(OrderFile.order_id)
            )
            file_counts = {order_id: count for order_id, count in counts.all()}

    if not records:
        await message.answer("Заявок в этом разделе нет.", reply_markup=admin_menu())
        return

    status_labels = {
        "lead": "Лид", "brief": "Черновик", "admin_review": "На проверке",
        "waiting_client_info": "Ждём клиента", "accepted": "Принята",
        "generating": "Генерация", "manual_pending": "Ручная загрузка",
        "demo_review": "Демо на проверке", "sending_demo": "Отправка демо",
        "demo_sent": "Демо отправлено", "paid": "Оплачена",
        "completed": "Завершена", "rejected": "Отклонена", "cancelled": "Отменена",
    }
    await message.answer(
        f"Заявки · показано {len(records)}"
        + (f" · фильтр: {', '.join(statuses)}" if statuses else ""),
        reply_markup=admin_menu(),
    )
    for offset in range(0, len(records), 5):
        chunk = records[offset:offset + 5]
        lines = []
        buttons = []
        for order, client in chunk:
            brief = dict(order.brief or {})
            who = client.first_name or "Имя не указано"
            if client.username:
                who += f" (@{client.username})"
            product = str(brief.get("product") or order.assistant_notes or "Товар не указан")
            product = " ".join(product.split())[:110]
            platform = brief.get("platform") or "Площадка не указана"
            slides = brief.get("slides")
            slide_label = f"{slides} слайд(ов)" if slides else "количество слайдов не указано"
            created = order.created_at.strftime("%d.%m.%Y %H:%M UTC") if order.created_at else "дата неизвестна"
            file_count = file_counts.get(order.id, 0)
            lines.append(
                f"№{order.id[:8]} · {status_labels.get(order.status, order.status)}\n"
                f"Клиент: {who}\n"
                f"Товар: {product}\n"
                f"Площадка: {platform} · {slide_label}\n"
                f"Материалов: {file_count} · {created}"
            )
            buttons.append([InlineKeyboardButton(
                text=f"Открыть {order.id[:8]}",
                callback_data=f"admin:open:{order.id}",
            )])
        await message.answer("\n\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith("admin:open:"))
async def admin_open_order(call: CallbackQuery):
    if not admin_only(call.from_user.id):
        await call.answer("Доступ только администратору.", show_alert=True)
        return
    order_id = call.data.split(":", 2)[2]
    found = await send_order_card(call.bot, call.message.chat.id, order_id)
    if found:
        await call.answer("Карточка заявки отправлена.")
    else:
        await call.answer("Заявка не найдена.", show_alert=True)


@router.message(Command("orders"))
async def orders(message: Message):
    if message.from_user.id != settings.admin_id:
        return
    await send_orders_list(message)


@router.message(Command("review"))
async def review_order(message: Message, command_text: str | None = None):
    if message.from_user.id != settings.admin_id:
        return
    parts = (command_text or message.text or "").split(maxsplit=1)
    prefix = parts[1].strip() if len(parts) > 1 else ""
    async with Session() as db:
        if prefix:
            if len(prefix) < 8:
                await message.answer("Для поиска заявки укажи минимум 8 символов ID.")
                return
            matches = list((await db.scalars(
                select(Order).where(Order.id.like(f"{prefix}%")).limit(2)
            )).all())
            if not matches:
                await message.answer("Заявка с таким ID не найдена.")
                return
            if len(matches) > 1:
                await message.answer("Найдено несколько заявок. Укажи больше символов ID.")
                return
            order = matches[0]
        else:
            order = await db.scalar(
                select(Order)
                .where(Order.status == "admin_review")
                .order_by(Order.created_at.desc())
                .limit(1)
            )
            if order is None:
                await message.answer("Нет заявок, ожидающих проверки.")
                return
    await send_order_card(message.bot, message.chat.id, order.id)


def parse_benefits(value) -> list[str]:
    source = "\n".join(str(x) for x in value) if isinstance(value, list) else str(value or "")
    items = [item.strip(" \t\r•-") for item in re.split(r"[\n;,]+", source)]
    return [item for item in items if item][:5]


def design_review_menu(order_id: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отправить демо клиенту", callback_data=f"design:send:{order_id}")]
    ])


def manual_generation_menu(order_id: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Повторить генерацию", callback_data=f"design:retry:{order_id}")],
        [InlineKeyboardButton(text="Продолжить вручную", callback_data=f"design:manual:{order_id}")],
    ])


async def admin_workflow_available(state: FSMContext, order_id: str) -> bool:
    current = await state.get_state()
    if current is None:
        return True
    data = await state.get_data()
    return current == AdminAction.manual_images.state and data.get("order_id") == order_id


def _slide_total(brief: dict) -> int:
    try:
        requested = int(brief.get("slides") or 1)
    except (TypeError, ValueError):
        requested = 1
    return max(1, min(requested, settings.max_generation_slides))


def _pending_slots(assets: list[dict], total_slides: int) -> list[dict]:
    existing = set()
    for item in assets:
        try:
            slide = int(item.get("slide", 0))
            variant = int(item.get("variant", 1))
            if slide and variant:
                existing.add((slide, variant))
        except (TypeError, ValueError):
            continue
    return [
        {"slide": slide, "variant": variant}
        for slide in range(1, total_slides + 1)
        for variant in (1, 2)
        if (slide, variant) not in existing
    ]


async def _download_order_inputs(bot: Bot, order_id: str, attachments: list[OrderFile]) -> list[Path]:
    input_dir = settings.storage_path / "orders" / order_id / "inputs"
    input_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index, item in enumerate(attachments, start=1):
        try:
            telegram_file = await bot.get_file(item.telegram_file_id)
            ext = Path(telegram_file.file_path or "").suffix.lower()
            if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
                ext = Path(item.original_name or "").suffix.lower()
            if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
                ext = ".jpg"
            path = input_dir / f"input_{index:02d}{ext}"
            if not path.exists():
                await bot.download_file(telegram_file.file_path, destination=path)
            if path.is_file() and path.stat().st_size:
                paths.append(path)
        except Exception:
            continue
    return paths


async def _set_order_status(order_id: str, status: str) -> None:
    async with Session() as db:
        order = await db.get(Order, order_id)
        if order:
            order.status = status
            await db.commit()


async def _prepare_asset(source_path: Path, final_path: Path, demo_path: Path, brief: dict) -> tuple[Path, Path]:
    final_path.parent.mkdir(parents=True, exist_ok=True)
    if settings.text_overlay_enabled:
        await asyncio.to_thread(
            apply_text_overlay, source_path, final_path,
            str(brief.get("product") or "")[:80],
            parse_benefits(
                f"{brief.get('advantages') or ''}\n{brief.get('required_text') or ''}"
            ),
        )
    elif source_path.resolve() != final_path.resolve():
        await asyncio.to_thread(shutil.copyfile, source_path, final_path)
    await asyncio.to_thread(
        make_watermarked_demo, final_path, demo_path,
        settings.demo_long_edge, settings.demo_watermark_text,
    )
    return final_path, demo_path


async def _persist_assets(
    order_id: str,
    new_assets: list[dict],
    *,
    api_call: bool = False,
    estimated_cost: float = 0.0,
    clear_pending: bool = False,
    failed_attempt: bool = False,
    uncertain_slide: int | None = None,
) -> dict:
    async with Session() as db:
        order = await db.get(Order, order_id)
        if order is None:
            raise ImagePipelineError("Заявка не найдена.")
        brief = dict(order.brief or {})
        assets = list(brief.get("design_assets", []))
        assets.extend(new_assets)
        brief["design_assets"] = assets
        metrics = dict(brief.get("generation_metrics", {}))
        metrics["api_calls"] = int(metrics.get("api_calls", 0)) + (1 if api_call else 0)
        metrics["images_generated"] = int(metrics.get("images_generated", 0)) + len(new_assets)
        metrics["estimated_cost_usd"] = round(
            float(metrics.get("estimated_cost_usd", 0.0)) + float(estimated_cost), 4
        )
        metrics["failed_attempts"] = int(metrics.get("failed_attempts", 0)) + (1 if failed_attempt else 0)
        if uncertain_slide is not None:
            metrics["uncertain_requests"] = int(metrics.get("uncertain_requests", 0)) + 1
            blocked = list(brief.get("manual_required_slides", []))
            if uncertain_slide not in blocked:
                blocked.append(uncertain_slide)
            brief["manual_required_slides"] = blocked
        brief["generation_metrics"] = metrics
        if clear_pending:
            brief.pop("generation_pending", None)
        order.brief = brief
        await db.commit()
        return brief


async def _send_album(
    bot: Bot, chat_id: int, paths: list[Path], caption: str = "", *, documents: bool = False
) -> list[int]:
    ids: list[int] = []
    for offset in range(0, len(paths), 10):
        chunk = paths[offset:offset + 10]
        if len(chunk) == 1:
            if documents:
                sent = await bot.send_document(chat_id, FSInputFile(chunk[0]), caption=caption if offset == 0 else None)
            else:
                sent = await bot.send_photo(chat_id, FSInputFile(chunk[0]), caption=caption if offset == 0 else None)
            ids.append(sent.message_id)
            continue
        if documents:
            media = [
                InputMediaDocument(media=FSInputFile(path), caption=caption if offset == 0 and i == 0 else None)
                for i, path in enumerate(chunk)
            ]
        else:
            media = [
                InputMediaPhoto(media=FSInputFile(path), caption=caption if offset == 0 and i == 0 else None)
                for i, path in enumerate(chunk)
            ]
        sent = await bot.send_media_group(chat_id, media=media)
        ids.extend(item.message_id for item in sent)
    return ids


async def _send_owner_review(bot: Bot, order_id: str, brief: dict, *, note: str = "Генерация завершена.") -> None:
    assets = sorted(
        list(brief.get("design_assets", [])),
        key=lambda item: (int(item.get("slide", 0)), int(item.get("variant", 0))),
    )
    originals = [Path(x["final_path"]) for x in assets if Path(x.get("final_path", "")).is_file()]
    demos = [Path(x["demo_path"]) for x in assets if Path(x.get("demo_path", "")).is_file()]
    if originals:
        await bot.send_message(settings.admin_id, f"Оригиналы без водяного знака · заявка {order_id[:8]}")
        await _send_album(bot, settings.admin_id, originals, documents=True)
    if demos:
        await bot.send_message(settings.admin_id, f"Предпросмотр демо с водяным знаком · заявка {order_id[:8]}")
        await _send_album(bot, settings.admin_id, demos)
    metrics = dict(brief.get("generation_metrics", {}))
    estimated = float(metrics.get("estimated_cost_usd", 0.0))
    summary = (
        f"{note}\nЗаявка: {order_id[:8]}\n"
        f"Подготовлено изображений: {len(originals)}\n"
        f"Успешных API-вызовов: {int(metrics.get('api_calls', 0))}\n"
        f"Оценка расходов API: USD {estimated:.2f}\n"
        f"Ошибочных попыток: {int(metrics.get('failed_attempts', 0))}\n"
        "Клиенту отправятся только копии с водяным знаком."
    )
    await bot.send_message(settings.admin_id, summary, reply_markup=design_review_menu(order_id))


async def _send_manual_bundle(
    bot: Bot,
    state: FSMContext,
    order_id: str,
    brief: dict,
    attachments: list[OrderFile],
    input_paths: list[Path],
    reason: str = "",
) -> None:
    total = _slide_total(brief)
    slots = _pending_slots(list(brief.get("design_assets", [])), total)
    if not slots:
        await _set_order_status(order_id, "demo_review")
        await _send_owner_review(bot, order_id, brief, note="Все варианты уже подготовлены.")
        return
    if not input_paths:
        await _set_order_status(order_id, "manual_pending")
        await bot.send_message(
            settings.admin_id,
            f"Заявка {order_id[:8]}: в заявке нет пригодного фото товара. "
            "Попроси клиента добавить фото товара, затем открой заявку через /review.",
            reply_markup=manual_generation_menu(order_id),
        )
        return

    def compact(key: str, limit: int) -> str:
        value = brief.get(key, "")
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(item) for item in value)
        return " ".join(str(value or "").split())[:limit] or "не указано"

    sequence = ", ".join(f"{x['slide']}/{x['variant']}" for x in slots)
    caption = (
        "Создай отдельные карточки товара для маркетплейса. "
        f"Товар: {compact('product', 100)}. "
        f"Преимущества: {compact('advantages', 80)}. "
        f"Обязательные тексты/факты: {compact('required_text', 80)}. "
        f"Стиль: {compact('style', 30)}. Цвета: {compact('colors', 30)}. "
        f"Пожелания к референсам: {compact('reference_texts', 70)}. "
        "Первое изображение во вложениях — сам товар, остальные — только референсы. "
        "Строго сохрани форму, пропорции, цвет, конструкцию, упаковку, логотип, этикетку "
        "и все надписи на товаре. Ничего не выдумывай. Не закрывай товар текстом. "
        "Сделай по два разных варианта для каждого слайда. "
        "Слайд 1 — обложка; следующие — отдельные инфографические преимущества. "
        f"Порядок файлов: {sequence}. Один файл на одну позицию."
    )[:1024]

    await state.clear()
    source_ids = await _send_album(bot, settings.admin_id, input_paths[:20], caption=caption, documents=True)
    await state.update_data(
        order_id=order_id,
        source_message_ids=source_ids,
        slots=slots,
        received_count=0,
        brief=brief,
    )
    await state.set_state(AdminAction.manual_images)
    extra = f"\nПричина ручного режима: {reason}" if reason else ""
    await bot.send_message(
        settings.admin_id,
        f"Пакет заявки {order_id[:8]} отправлен альбомом. Загрузи {len(slots)} готовых изображений "
        "ответом на любой элемент исходного альбома. Лучше отправлять как файлы без сжатия."
        f"{extra}",
        reply_markup=manual_generation_menu(order_id),
    )


async def _begin_generation(bot: Bot, state: FSMContext, order_id: str, *, force_manual: bool = False) -> None:
    if await state.get_state() is not None:
        await bot.send_message(
            settings.admin_id,
            "Сначала заверши или отмени текущий ручной сценарий. Генерацию нельзя запускать, пока активна загрузка изображений."
        )
        return
    async with Session() as db:
        result = await db.execute(
            update(Order).where(
                Order.id == order_id,
                Order.status.in_(["accepted", "manual_pending"]),
            ).values(status="generating")
        )
        if result.rowcount != 1:
            order = await db.get(Order, order_id)
            current = order.status if order else "не найдена"
            await bot.send_message(settings.admin_id, f"Заявка {order_id[:8]} уже обрабатывается или недоступна. Статус: {current}.")
            return
        await db.commit()
        order = await db.get(Order, order_id)
        brief = dict(order.brief or {})
        attachments = list((await db.scalars(
            select(OrderFile).where(OrderFile.order_id == order_id).order_by(OrderFile.id)
        )).all())

    await state.clear()
    total = _slide_total(brief)
    requested = int(brief.get("slides") or 1)
    if requested > settings.max_generation_slides:
        await bot.send_message(
            settings.admin_id,
            f"Лимит MAX_GENERATION_SLIDES={settings.max_generation_slides}: "
            f"будет обработано {settings.max_generation_slides} из {requested} слайдов."
        )
    input_paths = await _download_order_inputs(bot, order_id, attachments)
    if not input_paths:
        await _set_order_status(order_id, "manual_pending")
        await bot.send_message(
            settings.admin_id,
            f"Заявка {order_id[:8]}: не удалось получить исходные изображения. Генерация не запускалась.",
            reply_markup=manual_generation_menu(order_id),
        )
        return

    use_api = (
        not force_manual
        and settings.image_mode.strip().lower() == "auto"
        and bool(settings.openai_api_key)
    )
    if not use_api:
        await _set_order_status(order_id, "manual_pending")
        await _send_manual_bundle(bot, state, order_id, brief, attachments, input_paths)
        return

    metrics = dict(brief.get("generation_metrics", {}))
    current_cost = float(metrics.get("estimated_cost_usd", 0.0))
    unit_cost = max(0.0, float(settings.estimated_image_cost_usd))
    cost_cap = max(0.0, float(settings.max_order_cost_usd))
    blocked_slides = {int(x) for x in brief.get("manual_required_slides", [])}
    completed: dict[int, set[int]] = {}
    for asset in brief.get("design_assets", []):
        completed.setdefault(int(asset.get("slide", 0)), set()).add(int(asset.get("variant", 1)))

    fallback_reason = ""
    order_dir = settings.storage_path / "orders" / order_id
    order_dir.mkdir(parents=True, exist_ok=True)
    for slide in range(1, total + 1):
        variants = completed.get(slide, set())
        if len(variants) >= 2 or variants or slide in blocked_slides:
            continue
        reservation = unit_cost * 2
        if current_cost + reservation > cost_cap:
            fallback_reason = f"Достигнут лимит стоимости заказа USD {cost_cap:.2f}; оставшиеся слайды переданы в ручной режим."
            break

        raw_paths = [order_dir / f"slide_{slide:02d}_variant_{v:02d}_raw.png" for v in (1, 2)]
        final_paths = [order_dir / f"slide_{slide:02d}_variant_{v:02d}.png" for v in (1, 2)]
        demo_paths = [order_dir / f"slide_{slide:02d}_variant_{v:02d}_demo.jpg" for v in (1, 2)]
        idem_key = f"pervyi-slide:{order_id}:slide:{slide}:variants:1-2"

        async with Session() as db:
            order = await db.get(Order, order_id)
            updated = dict(order.brief or {})
            updated["generation_pending"] = {
                "slide": slide,
                "reserved_cost_usd": reservation,
                "idempotency_key": idem_key,
            }
            order.brief = updated
            await db.commit()

        request_succeeded = False
        try:
            await generate_two_images(
                api_key=settings.openai_api_key or "",
                base_url=settings.openai_base_url,
                model=settings.openai_image_model,
                prompt=build_product_prompt(brief, slide, total),
                image_paths=input_paths,
                output_paths=raw_paths,
                idempotency_key=idem_key,
                timeout_seconds=settings.generation_timeout_seconds,
                max_attempts=3,
                size=settings.openai_image_size,
                quality=settings.openai_image_quality,
            )
            request_succeeded = True
            new_assets = []
            for variant, raw_path in enumerate(raw_paths, start=1):
                await _prepare_asset(raw_path, final_paths[variant - 1], demo_paths[variant - 1], brief)
                new_assets.append({
                    "slide": slide,
                    "variant": variant,
                    "final_path": str(final_paths[variant - 1]),
                    "demo_path": str(demo_paths[variant - 1]),
                    "source": "openai_images",
                })
            brief = await _persist_assets(
                order_id, new_assets, api_call=True,
                estimated_cost=reservation, clear_pending=True,
            )
            current_cost += reservation
            completed[slide] = {1, 2}
        except ImageGenerationError as exc:
            brief = await _persist_assets(
                order_id, [], api_call=exc.request_succeeded,
                clear_pending=True, failed_attempt=True,
                uncertain_slide=slide if exc.uncertain else None,
                estimated_cost=reservation if (exc.uncertain or exc.request_succeeded) else 0.0,
            )
            if exc.uncertain:
                current_cost += reservation
            fallback_reason = str(exc)
            break
        except Exception:
            brief = await _persist_assets(
                order_id, [], api_call=request_succeeded,
                clear_pending=True, failed_attempt=True,
                uncertain_slide=slide, estimated_cost=reservation,
            )
            current_cost += reservation
            fallback_reason = "Ошибка обработки результата; слайд переведён в ручной режим."
            break

    missing = _pending_slots(list(brief.get("design_assets", [])), total)
    if fallback_reason or missing:
        await _set_order_status(order_id, "manual_pending")
        await _send_manual_bundle(
            bot, state, order_id, brief, attachments, input_paths,
            reason=fallback_reason or "Не все варианты слайдов были созданы автоматически.",
        )
        return

    await _set_order_status(order_id, "demo_review")
    await _send_owner_review(bot, order_id, brief)


@router.message(Command("generate"))
async def generate_order_design(message: Message, state: FSMContext):
    if message.from_user.id != settings.admin_id:
        return
    parts = (message.text or "").split(maxsplit=1)
    prefix = parts[1].strip() if len(parts) > 1 else ""
    async with Session() as db:
        if prefix:
            matches = list((await db.scalars(select(Order).where(Order.id.like(f"{prefix}%")).limit(2))).all())
            if not matches:
                await message.answer("Заявка не найдена.")
                return
            if len(matches) > 1:
                await message.answer("Найдено несколько заявок. Укажи больше символов ID.")
                return
            order_id = matches[0].id
        else:
            order = await db.scalar(
                select(Order).where(Order.status.in_(["accepted", "manual_pending"]))
                .order_by(Order.created_at.desc()).limit(1)
            )
            if order is None:
                await message.answer("Нет принятых заявок, готовых к генерации.")
                return
            order_id = order.id
    await _begin_generation(message.bot, state, order_id)


@router.callback_query(F.data.startswith("design:"))
async def process_design_action(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != settings.admin_id:
        await call.answer("Только администратор может запускать генерацию и одобрять отправку.", show_alert=True)
        return
    try:
        _, action, order_id = call.data.split(":", 2)
    except (AttributeError, ValueError):
        await call.answer("Некорректное действие.", show_alert=True)
        return

    if action in {"create", "retry"}:
        await call.answer("Запускаю обработку заявки.")
        await _begin_generation(call.bot, state, order_id)
        return

    if action == "manual":
        if not await admin_workflow_available(state, order_id):
            await call.answer(
                "Сначала заверши или отмени ручную загрузку текущего заказа.",
                show_alert=True,
            )
            return
        async with Session() as db:
            order = await db.get(Order, order_id)
            if order is None or order.status != "manual_pending":
                await call.answer("Заявка не ожидает ручной обработки.", show_alert=True)
                return
            brief = dict(order.brief or {})
            attachments = list((await db.scalars(
                select(OrderFile).where(OrderFile.order_id == order_id).order_by(OrderFile.id)
            )).all())
        input_paths = await _download_order_inputs(call.bot, order_id, attachments)
        await call.answer("Готовлю пакет для ручного режима.")
        await _send_manual_bundle(call.bot, state, order_id, brief, attachments, input_paths)
        return

    if action != "send":
        await call.answer("Неизвестное действие.", show_alert=True)
        return

    async with Session() as db:
        result = await db.execute(
            update(Order).where(Order.id == order_id, Order.status == "demo_review").values(status="sending_demo")
        )
        if result.rowcount != 1:
            await call.answer("Демо уже отправляется или заявка не готова.", show_alert=True)
            return
        await db.commit()
        order = await db.get(Order, order_id)
        brief = dict(order.brief or {})
        client = await db.get(Client, order.client_id)
        client_telegram_id = client.telegram_id if client else None
        assets = sorted(
            list(brief.get("design_assets", [])),
            key=lambda item: (int(item.get("slide", 0)), int(item.get("variant", 0))),
        )

    if not client_telegram_id or not assets:
        await _set_order_status(order_id, "demo_review")
        await call.answer("Не найден клиент или отсутствуют готовые демо.", show_alert=True)
        return
    try:
        await call.bot.send_message(
            client_telegram_id,
            f"Подготовлено демо по заявке №{order_id[:8]}. Это предварительные изображения с водяным знаком. "
            "Напиши, что нужно поправить. Оригиналы без водяного знака передаются только после согласования и оплаты."
        )
        for asset in assets:
            demo_path = Path(asset.get("demo_path", ""))
            if not demo_path.is_file():
                raise ImagePipelineError("Демо-файл отсутствует.")
            await call.bot.send_photo(
                client_telegram_id, FSInputFile(demo_path),
                caption=f"Демо · слайд {asset.get('slide')} · вариант {asset.get('variant')}",
            )
    except Exception:
        await _set_order_status(order_id, "demo_review")
        await call.message.answer("Не удалось доставить демо. Статус сохранён для повторной проверки.")
        await call.answer("Отправка не удалась.", show_alert=True)
        return

    await _set_order_status(order_id, "demo_sent")
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("Демо отправлено клиенту.")
    await call.message.answer(f"Демо по заявке {order_id[:8]} отправлено. Клиент получил только версии с водяным знаком.")


@router.message(AdminAction.manual_images, F.photo | F.document)
async def receive_manual_variants(message: Message, state: FSMContext):
    if message.from_user.id != settings.admin_id:
        return
    data = await state.get_data()
    order_id = data.get("order_id")
    source_ids = set(data.get("source_message_ids", []))
    slots = list(data.get("slots", []))
    if not order_id or not slots:
        await state.clear()
        await message.answer("Режим ручной загрузки закрыт. Открой заявку через /review.")
        return
    reply_id = message.reply_to_message.message_id if message.reply_to_message else None
    if reply_id not in source_ids:
        await message.answer("Отправь изображение ответом на исходный альбом с промтом и материалами.")
        return

    if message.photo:
        media = message.photo[-1]
        file_id, file_size, suggested_name = media.file_id, media.file_size, "manual_variant.jpg"
    else:
        media = message.document
        if media.mime_type not in {"image/jpeg", "image/png", "image/webp"}:
            await message.answer("Принимаю только JPG, PNG и WEBP.")
            return
        file_id, file_size, suggested_name = media.file_id, media.file_size, media.file_name or "manual_variant.png"
    if file_size and file_size > settings.max_file_mb * 1024 * 1024:
        await message.answer(f"Файл превышает лимит {settings.max_file_mb} МБ.")
        return

    async with Session() as db:
        order = await db.get(Order, order_id)
        if order is None or order.status not in {"manual_pending", "generating"}:
            await state.clear()
            await message.answer("Заявка больше не ожидает ручные изображения.")
            return
        brief = dict(order.brief or {})
        assets = list(brief.get("design_assets", []))
        duplicate_upload = any(
            int(asset.get("source_message_id", 0)) == message.message_id
            for asset in assets
        )
        assigned = {
            (int(asset.get("slide", 0)), int(asset.get("variant", 1)))
            for asset in assets
        }

    remaining = [
        slot for slot in slots
        if (int(slot["slide"]), int(slot["variant"])) not in assigned
    ]
    if duplicate_upload:
        if not remaining:
            await state.clear()
            await _set_order_status(order_id, "demo_review")
            await _send_owner_review(
                message.bot, order_id, brief,
                note="Ручная загрузка восстановлена после повторного Telegram-сообщения.",
            )
        else:
            await message.answer("Это изображение уже было принято. Дубликат пропущен.")
        return
    if not remaining:
        await state.clear()
        await message.answer("Все варианты по этой заявке уже сохранены. Открой результаты через /review.")
        return
    slot = remaining[0]

    order_dir = settings.storage_path / "orders" / order_id
    incoming_dir = order_dir / "manual_uploads"
    incoming_dir.mkdir(parents=True, exist_ok=True)
    try:
        telegram_file = await message.bot.get_file(file_id)
        ext = Path(telegram_file.file_path or "").suffix.lower()
        if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
            ext = Path(suggested_name).suffix.lower()
        if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
            ext = ".jpg"
        source_path = incoming_dir / f"upload_{message.message_id}{ext}"
        await message.bot.download_file(telegram_file.file_path, destination=source_path)
        final_ext = ".png" if settings.text_overlay_enabled else ext
        final_path = order_dir / f"slide_{slot['slide']:02d}_variant_{slot['variant']:02d}{final_ext}"
        demo_path = order_dir / f"slide_{slot['slide']:02d}_variant_{slot['variant']:02d}_demo.jpg"
        await _prepare_asset(source_path, final_path, demo_path, data.get("brief", {}))
        asset = {
            "slide": int(slot["slide"]),
            "variant": int(slot["variant"]),
            "final_path": str(final_path),
            "demo_path": str(demo_path),
            "source": "manual",
            "source_message_id": message.message_id,
        }
        updated_brief = await _persist_assets(order_id, [asset])
        updated_assets = list(updated_brief.get("design_assets", []))
        updated_slots = {
            (int(item.get("slide", 0)), int(item.get("variant", 1)))
            for item in updated_assets
        }
        received = sum(
            1 for item in slots
            if (int(item["slide"]), int(item["variant"])) in updated_slots
        )
        await state.update_data(received_count=received)
        await message.answer(
            f"Вариант {slot['variant']} слайда {slot['slide']} принят ({received}/{len(slots)})."
        )
        if received >= len(slots):
            await state.clear()
            await _set_order_status(order_id, "demo_review")
            await _send_owner_review(
                message.bot,
                order_id,
                updated_brief,
                note="Ручная загрузка завершена. Оригиналы сохранены без водяного знака.",
            )
    except Exception:
        await message.answer(
            "Не удалось обработать файл. Попробуй отправить изображение ещё раз ответом на исходный альбом."
        )


@router.message(AdminAction.manual_images)
async def manual_upload_help(message: Message, state: FSMContext):
    if message.from_user.id != settings.admin_id:
        return
    if (message.text or "").strip().casefold() in {"отмена", "/cancel"}:
        await state.clear()
        await message.answer("Ручная загрузка отменена. Заявка сохранена; открыть её можно через /review.")
        return
    await message.answer("Ожидаю изображение ответом на исходный альбом. Отправляй готовые варианты по одному.")


@router.message(Command("order"))
async def create_assistant_order(message: Message):
    if message.from_user.id != settings.admin_id:
        return
    anchor_message = message.reply_to_message
    if anchor_message is None:
        await message.answer("Перешли сообщения клиента боту, затем ответь /order на одно из них.")
        return
    async with Session() as db:
        anchor = await db.scalar(select(AssistantMaterial).where(
            AssistantMaterial.admin_chat_id == message.chat.id,
            AssistantMaterial.admin_message_id == anchor_message.message_id,
            AssistantMaterial.consumed_at.is_(None)))
        if anchor is None:
            await message.answer("Не нашёл пересланное сообщение. Сначала перешли его боту, затем ответь /order.")
            return
        if not anchor.source_user_id:
            await message.answer("Telegram скрыл ID отправителя. Попроси клиента написать боту напрямую.")
            return
        cutoff = now() - timedelta(minutes=30)
        items = list((await db.scalars(select(AssistantMaterial).where(
            AssistantMaterial.admin_chat_id == message.chat.id,
            AssistantMaterial.source_user_id == anchor.source_user_id,
            AssistantMaterial.consumed_at.is_(None),
            AssistantMaterial.created_at >= cutoff).order_by(AssistantMaterial.created_at))).all())
        client = await db.scalar(select(Client).where(Client.telegram_id == anchor.source_user_id))
        if client is None:
            client = Client(telegram_id=anchor.source_user_id, username=anchor.source_username)
            db.add(client)
            await db.flush()
        notes = "\n".join(x.text_content for x in items if x.text_content)
        order = Order(client_id=client.id, status="brief", source="assistant",
                      assistant_notes=notes or "Материалы через режим ассистента")
        db.add(order)
        await db.flush()
        for item in items:
            if item.telegram_file_id:
                db.add(OrderFile(order_id=order.id, telegram_file_id=item.telegram_file_id,
                                 file_type=item.file_type or "document", original_name=item.original_name))
            item.consumed_at = now()
        await db.commit()
        order_id = order.id
        client_id = client.telegram_id
    await message.answer(f"Создан черновик заказа {order_id[:8]}. Клиент: {client_id}. Бот клиенту не писал.")


@router.message(F.forward_origin)
async def collect_forward(message: Message):
    if message.from_user.id != settings.admin_id:
        return
    origin = message.forward_origin
    sender = getattr(origin, "sender_user", None)
    file_id = file_type = filename = None
    if message.photo:
        file_id, file_type = message.photo[-1].file_id, "photo"
    elif message.document:
        file_id, file_type, filename = message.document.file_id, "document", message.document.file_name
    async with Session() as db:
        db.add(AssistantMaterial(
            admin_chat_id=message.chat.id, admin_message_id=message.message_id,
            source_user_id=sender.id if sender else None,
            source_username=sender.username if sender else None,
            text_content=(message.text or message.caption or "")[:4000] or None,
            telegram_file_id=file_id, file_type=file_type, original_name=filename))
        await db.commit()


@router.message(Command("paid"))
async def paid(message: Message):
    if not admin_only(message.from_user.id): return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Формат: /paid ID_ЗАЯВКИ СУММА [комментарий]")
        return
    prefix, raw_amount = parts[1], parts[2].split(maxsplit=1)[0]
    note = parts[2][len(raw_amount):].strip() or None
    try: amount = int(raw_amount.replace("₽", "").replace("руб.", ""))
    except ValueError:
        await message.answer("Сумма должна быть целым числом в рублях.")
        return
    if amount <= 0:
        await message.answer("Сумма должна быть больше нуля.")
        return
    async with Session() as db:
        matches = list((await db.scalars(select(Order).where(Order.id.like(f"{prefix}%")).limit(2))).all())
        if len(matches) != 1:
            await message.answer("Заявка не найдена или ID неоднозначен.")
            return
        order = matches[0]
        order.status = "paid"
        db.add(Payment(order_id=order.id, amount=amount, note=note))
        await db.commit()
        client = await db.get(Client, order.client_id)
        client_id = client.telegram_id if client else None
    await message.answer(f"Оплата зарегистрирована: заявка {order.id[:8]}, {amount} RUB.")
    if client_id:
        try: await message.bot.send_message(client_id, f"Оплата по заявке №{order.id[:8]} подтверждена.")
        except Exception: pass


@router.message(Command("remind"))
async def remind(message: Message):
    if not admin_only(message.from_user.id): return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Формат: /remind ID_ЗАЯВКИ")
        return
    prefix = parts[1].strip()
    async with Session() as db:
        matches = list((await db.scalars(select(Order).where(Order.id.like(f"{prefix}%")).limit(2))).all())
        if len(matches) != 1:
            await message.answer("Заявка не найдена или ID неоднозначен.")
            return
        order = matches[0]
        client = await db.get(Client, order.client_id)
        client_id = client.telegram_id if client else None
    if not client_id:
        await message.answer("У заявки не найден клиент.")
        return
    try:
        await message.bot.send_message(client_id, f"Напоминание по заявке №{order.id[:8]}. Напиши нам, если хочешь продолжить оформление.")
        await message.answer(f"Напоминание отправлено клиенту по заявке {order.id[:8]}.")
    except Exception:
        await message.answer("Не удалось доставить напоминание клиенту.")


@router.message(Command("stop"))
async def stop(message: Message):
    async with Session() as db:
        client = await get_client(db, message.from_user)
        client.stopped = True
        await db.commit()
    await message.answer("Инициативные сообщения отключены. Для возобновления напиши /start.")


@router.message(Command("forget"))
async def forget(message: Message):
    async with Session() as db:
        client = await db.scalar(select(Client).where(Client.telegram_id == message.from_user.id))
        if client:
            ids = list((await db.scalars(select(Order.id).where(Order.client_id == client.id))).all())
            if ids:
                await db.execute(delete(OrderFile).where(OrderFile.order_id.in_(ids)))
                await db.execute(delete(Order).where(Order.id.in_(ids)))
            await db.execute(delete(AssistantMaterial).where(AssistantMaterial.source_user_id == message.from_user.id))
            await db.delete(client)
            await db.commit()
    await message.answer("Данные профиля и заявки удалены из базы. Telegram-вложения физически не копируются в хранилище на этом этапе.")


@router.message(F.text)
async def forward_client_clarification(message: Message):
    # Перехватываем обычный текст только когда от этого клиента действительно ждут уточнение.
    async with Session() as db:
        client = await db.scalar(select(Client).where(Client.telegram_id == message.from_user.id))
        if client is None:
            return
        order = await db.scalar(
            select(Order)
            .where(Order.client_id == client.id, Order.status == "waiting_client_info")
            .order_by(Order.created_at.desc())
            .limit(1)
        )
        if order is None:
            return
        order_id = order.id
        previous = order.assistant_notes or ""
        order.assistant_notes = previous + f"\nОтвет клиента: {message.text[:3000]}"
        order.status = "admin_review"
        await db.commit()

    await message.answer(f"Ответ по заявке №{order_id[:8]} передан администратору.")
    await send_order_card(message.bot, settings.admin_id, order_id)


async def recover_interrupted_orders() -> list[str]:
    recovered: list[str] = []
    async with Session() as db:
        rows = list((await db.scalars(
            select(Order).where(Order.status.in_(["generating", "sending_demo"]))
        )).all())
        for order in rows:
            order_id = order.id
            brief = dict(order.brief or {})
            if order.status == "generating":
                pending = brief.pop("generation_pending", None)
                if pending:
                    try:
                        slide = int(pending.get("slide", 0))
                        reservation = max(0.0, float(pending.get("reserved_cost_usd", 0.0)))
                    except (TypeError, ValueError):
                        slide, reservation = 0, 0.0
                    if slide:
                        already_saved = any(
                            int(asset.get("slide", 0)) == slide
                            for asset in brief.get("design_assets", [])
                        )
                        if not already_saved:
                            blocked = list(brief.get("manual_required_slides", []))
                            if slide not in blocked:
                                blocked.append(slide)
                            brief["manual_required_slides"] = blocked
                            metrics = dict(brief.get("generation_metrics", {}))
                            metrics["uncertain_requests"] = int(metrics.get("uncertain_requests", 0)) + 1
                            metrics["estimated_cost_usd"] = round(
                                float(metrics.get("estimated_cost_usd", 0.0)) + reservation, 4
                            )
                            brief["generation_metrics"] = metrics
                order.brief = brief
                order.status = "manual_pending"
            else:
                order.status = "demo_review"
            recovered.append(order_id[:8])
        if rows:
            await db.commit()
    return recovered


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    recovered = await recover_interrupted_orders()
    bot = Bot(settings.bot_token)
    if recovered:
        try:
            await bot.send_message(
                settings.admin_id,
                "Восстановлены незавершённые заявки: " + ", ".join(recovered) +
                ". Проверь их через /orders; незавершённые API-запросы повторно автоматически не запускаются."
            )
        except Exception:
            pass
    dp = Dispatcher(storage=DatabaseStorage(), events_isolation=SimpleEventIsolation())
    dp.include_router(router)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await dp.storage.close()
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
