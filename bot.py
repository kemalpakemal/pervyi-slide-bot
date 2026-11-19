import asyncio
import enum
import os
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
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, JSON, String, Text, delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Settings(BaseSettings):
    bot_token: str
    admin_id: int
    database_url: str = "sqlite+aiosqlite:///./card_studio.db"
    storage_path: Path = Path("./data")
    max_file_mb: int = 15
    max_files_per_order: int = 12
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


router = Router()


def menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Заказать карточку"), KeyboardButton(text="Примеры работ")],
        [KeyboardButton(text="Цены"), KeyboardButton(text="Связаться с автором")],
    ], resize_keyboard=True)


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


async def send_order_card(bot: Bot, admin_chat_id: int, order_id: str):
    async with Session() as db:
        order = await db.get(Order, order_id)
        if order is None:
            return False
        client = await db.get(Client, order.client_id)
        attachments = list((await db.scalars(
            select(OrderFile).where(OrderFile.order_id == order_id)
        )).all())
        brief = dict(order.brief or {})
        client_id = client.telegram_id if client else 0
        username = client.username if client else None
        status = order.status
        source = order.source
        notes = order.assistant_notes
        created_at = order.created_at

    lines = [
        f"Заявка: {order_id}",
        f"Статус: {status}",
        f"Клиент: @{username}" if username else f"Клиент ID: {client_id}",
        f"Telegram ID: {client_id}",
        f"Источник: {source}",
    ]
    for key, label in (
        ("platform", "Площадка"), ("product", "Товар"), ("advantages", "Преимущества"),
        ("required_text", "Тексты"), ("style", "Стиль"), ("colors", "Цвета"),
        ("slides", "Слайдов"), ("deadline", "Срок"),
    ):
        value = brief.get(key)
        if value not in (None, ""):
            lines.append(f"{label}: {value}")
    if notes:
        lines.append(f"Дополнительная информация: {notes}")
    lines.append(f"Файлов: {len(attachments)}")
    keyboard = order_action_menu(order_id) if status == "admin_review" else None
    await bot.send_message(admin_chat_id, "\\n".join(lines), reply_markup=keyboard)
    for item in attachments:
        try:
            if item.file_type == "photo":
                await bot.send_photo(admin_chat_id, item.telegram_file_id)
            else:
                await bot.send_document(admin_chat_id, item.telegram_file_id)
        except Exception:
            await bot.send_message(admin_chat_id, f"Не удалось повторно отправить вложение заявки {order_id[:8]}.")
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
    if message.from_user.id == settings.admin_id:
        welcome += (
            "\\n\\nРежим администратора активен. "
            "Команды управления: /orders, /health, /order, /paid, /remind. "
            "При этом ты можешь пользоваться ботом как обычный клиент и проверять оформление заказа."
        )
    await message.answer(welcome, reply_markup=menu())


@router.message(F.text == "Заказать карточку")
async def begin(message: Message, state: FSMContext):
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
    await message.answer("Отправь фото товара и референсы. Можно несколько сообщений. Когда закончишь, напиши «Готово».")


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
        order.assistant_notes = (previous + "\\n" if previous else "") + f"Уточнение от студии: {text}"
        await db.commit()

    await state.clear()
    if client_telegram_id:
        try:
            await message.bot.send_message(
                client_telegram_id,
                f"По заявке №{order_id[:8]} нужно уточнение:\\n\\n{text}\\n\\n"
                "Ответь следующим сообщением в этом чате — я передам ответ администратору."
            )
            await message.answer("Уточнение отправлено клиенту. Жду его ответ.")
        except Exception:
            await message.answer("Статус заявки сохранён, но Telegram не доставил сообщение клиенту.")
    else:
        await message.answer("У заявки не найден Telegram ID клиента.")


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
        order.assistant_notes = previous + f"\\nОтвет клиента: {message.text[:3000]}"
        order.status = "admin_review"
        await db.commit()

    await message.answer(f"Ответ по заявке №{order_id[:8]} передан администратору.")
    await send_order_card(message.bot, settings.admin_id, order_id)


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


@router.message(Command("orders"))
async def orders(message: Message):
    if message.from_user.id != settings.admin_id:
        return
    async with Session() as db:
        rows = list((await db.scalars(select(Order).order_by(Order.created_at.desc()).limit(20))).all())
    if not rows:
        await message.answer("Заказов пока нет.")
        return
    await message.answer("Последние заказы:\n" + "\n".join(f"{x.id[:8]} · {x.status} · {x.source}" for x in rows))


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
    if message.from_user.id == settings.admin_id:
        await message.answer("Подтверждение оплаты будет добавлено на этапе 4. Сейчас оплата не подтверждается и оригиналы не выдаются.")


@router.message(Command("remind"))
async def remind(message: Message):
    if message.from_user.id == settings.admin_id:
        await message.answer("Напоминания будут добавлены на этапе 4. Сообщения клиентам автоматически не отправляются.")


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


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    bot = Bot(settings.bot_token)
    dp = Dispatcher(storage=DatabaseStorage())
    dp.include_router(router)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await dp.storage.close()
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
