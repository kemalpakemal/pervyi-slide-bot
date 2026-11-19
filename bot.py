import asyncio
import logging
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.types import (
    CallbackQuery, ErrorEvent, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove, User,
)
from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, JSON, String, Text, delete, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

import logic

BASE_DIR = Path(__file__).resolve().parent
log = logging.getLogger("bot")


def setup_logging() -> None:
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(
        stream=sys.stdout, level=level, force=True,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


class Settings(BaseSettings):
    bot_token: str
    admin_id: int
    database_url: str = "sqlite+aiosqlite:///./card_studio.db"
    storage_path: Path = Path("./data")
    max_file_mb: int = 15
    max_files_per_order: int = 12
    enable_lead_radar: bool = False
    image_provider: str = ""
    llm_provider: str = ""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def db_url(self) -> str:
        return logic.normalize_db_url(self.database_url)


# Инициализируются в main(), чтобы ошибки конфигурации попадали в логи.
settings: Settings
engine = None
Session: async_sessionmaker


def now():
    return datetime.now(timezone.utc)


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


router = Router()
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

# Порядок шагов брифа: (состояние, ключ, лимит символов, вопрос)
TEXT_STEPS = [
    (Brief.product, "product", 300, "Что за товар? Укажи название, модель и разновидность."),
    (Brief.advantages, "advantages", 1000, "Назови 3–5 главных преимуществ товара."),
    (Brief.required_text, "required_text", 1000,
     "Какие тексты или характеристики обязательно разместить? Если нет — напиши «на усмотрение студии»."),
    (Brief.style, "style", 300, "Какой стиль нужен? Например: минимализм, премиальный, яркий рекламный, технологичный."),
    (Brief.colors, "colors", 300, "Какие цвета использовать или избегать?"),
]
SLIDES_Q = "Сколько слайдов требуется? Укажи число от 1 до 20."
DEADLINE_Q = "К какому сроку нужна работа? Напиши дату или «не срочно». Срок подтвердит администратор."
MATERIALS_Q = ("Отправь фото товара и референсы (изображения JPG/PNG/WEBP, ссылки или описание). "
               "Можно несколькими сообщениями. Когда закончишь, нажми «Готово».")
PLATFORM_Q = "Для какой площадки нужна карточка?"
ORDER = [Brief.platform, Brief.product, Brief.advantages, Brief.required_text, Brief.style,
         Brief.colors, Brief.slides, Brief.deadline, Brief.materials]
QUESTIONS = {Brief.platform: PLATFORM_Q, Brief.slides: SLIDES_Q, Brief.deadline: DEADLINE_Q,
             Brief.materials: MATERIALS_Q, **{st: q for st, _, _, q in TEXT_STEPS}}
TEXT_KEYS = {st: (key, limit) for st, key, limit, _ in TEXT_STEPS}

_locks: dict[int, asyncio.Lock] = {}


def user_lock(user_id: int) -> asyncio.Lock:
    """Обновления одного пользователя приходят параллельно (альбомы, двойные клики)."""
    lock = _locks.get(user_id)
    if lock is None:
        lock = _locks[user_id] = asyncio.Lock()
    return lock


def is_admin(user: User | None) -> bool:
    return user is not None and user.id == settings.admin_id


def menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Заказать карточку"), KeyboardButton(text="Примеры работ")],
        [KeyboardButton(text="Цены"), KeyboardButton(text="Связаться с автором")],
    ], resize_keyboard=True)


def platform_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="WB"), KeyboardButton(text="Ozon"), KeyboardButton(text="Avito")],
        [KeyboardButton(text="Отмена")],
    ], resize_keyboard=True)


def step_menu(done: bool = False):
    rows = []
    if done:
        rows.append([KeyboardButton(text="Готово")])
    rows.append([KeyboardButton(text="Назад"), KeyboardButton(text="Отмена")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def cancel_menu():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Готово"), KeyboardButton(text="Отмена")]],
                               resize_keyboard=True)


def confirm_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подтвердить бриф", callback_data="brief:confirm")],
        [InlineKeyboardButton(text="Заполнить заново", callback_data="brief:restart")],
    ])


def kb_for(state) -> ReplyKeyboardMarkup:
    if state == Brief.platform:
        return platform_menu()
    return step_menu(done=state == Brief.materials)


async def ask(message: Message, state_obj, fsm: FSMContext) -> None:
    await fsm.set_state(state_obj)
    await message.answer(QUESTIONS[state_obj], reply_markup=kb_for(state_obj))


async def get_client(db, user):
    client = await db.scalar(select(Client).where(Client.telegram_id == user.id))
    if client is None:
        client = Client(telegram_id=user.id, username=user.username, first_name=user.first_name)
        db.add(client)
        await db.flush()
    else:
        client.username, client.first_name = user.username, user.first_name
    return client


async def notify_admin(bot: Bot, text_: str) -> bool:
    try:
        await bot.send_message(settings.admin_id, text_[:4000])
        return True
    except TelegramAPIError:
        log.exception("Не удалось отправить сообщение администратору")
        return False


# ---------- Команды (регистрируются раньше шагов FSM, чтобы работать в любом состоянии) ----------

@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    async with Session() as db:
        client = await get_client(db, message.from_user)
        client.stopped = False
        await db.commit()
    welcome = (
        "Здравствуйте! Я ассистент студии «Первый слайд» (не человек). "
        "Помогу оформить заявку на создание карточки товара для WB, Ozon или Avito. "
        "Каждый результат проверяет владелец студии."
    )
    if is_admin(message.from_user):
        welcome += (
            "\n\nРежим администратора активен. "
            "Команды: /orders, /health, /order, /paid, /remind. "
            "Бот можно использовать и как обычный клиент для проверки оформления заказа."
        )
    await message.answer(welcome, reply_markup=menu())


@router.message(Command("cancel"))
@router.message(F.text == "Отмена")
async def cancel(message: Message, state: FSMContext):
    if await state.get_state() is None:
        await message.answer("Нечего отменять. Выбери действие в меню.", reply_markup=menu())
        return
    await state.clear()
    await message.answer("Отменено. Ничего не отправлено.", reply_markup=menu())


@router.message(F.text == "Назад", StateFilter(*ORDER))
async def back(message: Message, state: FSMContext):
    cur = await state.get_state()
    names = [s.state for s in ORDER]
    idx = names.index(cur) if cur in names else 0
    if idx == 0:
        await state.clear()
        await message.answer("Заявка отменена.", reply_markup=menu())
        return
    await ask(message, ORDER[idx - 1], state)


@router.message(F.text == "Назад", Brief.confirmation)
async def back_from_confirm(message: Message, state: FSMContext):
    await ask(message, Brief.materials, state)


@router.message(Command("health"))
async def health(message: Message):
    if not is_admin(message.from_user):
        return
    lines = ["Бот отвечает."]
    try:
        async with Session() as db:
            await db.execute(select(1))
        lines.append(f"БД: OK ({engine.dialect.name})")
    except Exception as exc:
        log.exception("health: БД недоступна")
        lines.append(f"БД: ОШИБКА ({type(exc).__name__})")
    ok, info = check_storage(settings.storage_path)
    lines.append(f"Хранилище {settings.storage_path}: {'OK, запись работает' if ok else 'ОШИБКА — ' + info}")
    lines.append("Лимиты: файл {} МБ, файлов в заказе {}".format(settings.max_file_mb, settings.max_files_per_order))
    lines.append("Опционально (значения не показываются):")
    lines.append(f"• IMAGE_PROVIDER: {settings.image_provider or 'не задан'}")
    lines.append(f"• LLM_PROVIDER: {settings.llm_provider or 'не задан'}")
    lines.append(f"• ENABLE_LEAD_RADAR: {'вкл' if settings.enable_lead_radar else 'выкл'}")
    keys = [k for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
                        "REPLICATE_API_TOKEN", "STABILITY_API_KEY", "FAL_KEY", "OPENROUTER_API_KEY")
            if os.getenv(k)]
    lines.append("• Ключи провайдеров: " + (", ".join(keys) if keys else "не найдены"))
    lines.append("Этап 1: генерация и LLM пока не используются.")
    await message.answer("\n".join(lines))


def check_storage(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path, prefix=".healthcheck-", delete=True) as f:
            f.write(b"ok")
            f.flush()
        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


@router.message(Command("orders"))
async def orders(message: Message):
    if not is_admin(message.from_user):
        return
    async with Session() as db:
        rows = list((await db.scalars(select(Order).order_by(Order.created_at.desc()).limit(20))).all())
    if not rows:
        await message.answer("Заказов пока нет.")
        return
    await message.answer("Последние заказы:\n" + "\n".join(f"{x.id[:8]} · {x.status} · {x.source}" for x in rows))


@router.message(Command("order"))
async def create_assistant_order(message: Message):
    if not is_admin(message.from_user):
        return
    anchor_message = message.reply_to_message
    if anchor_message is None:
        await message.answer("Перешли сообщения клиента боту, затем ответь /order на одно из них.")
        return
    async with user_lock(message.from_user.id):
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
            if anchor not in items:
                items.insert(0, anchor)
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
            order_id, client_tg = order.id, client.telegram_id
    log.info("Создан черновик заказа %s (assistant), материалов: %d", order_id[:8], len(items))
    await message.answer(f"Создан черновик заказа {order_id[:8]}. Клиент: {client_tg}. Бот клиенту не писал.")


@router.message(Command("paid"))
async def paid(message: Message):
    if is_admin(message.from_user):
        await message.answer("Подтверждение оплаты будет добавлено на этапе 4. Сейчас оплата не подтверждается и оригиналы не выдаются.")


@router.message(Command("remind"))
async def remind(message: Message):
    if is_admin(message.from_user):
        await message.answer("Напоминания будут добавлены на этапе 4. Сообщения клиентам автоматически не отправляются.")


@router.message(Command("stop"))
async def stop(message: Message):
    async with Session() as db:
        client = await get_client(db, message.from_user)
        client.stopped = True
        await db.commit()
    await message.answer("Инициативные сообщения отключены. Для возобновления напиши /start.")


@router.message(Command("forget"))
async def forget(message: Message, state: FSMContext):
    uid = message.from_user.id
    async with Session() as db:
        client = await db.scalar(select(Client).where(Client.telegram_id == uid))
        if client:
            ids = list((await db.scalars(select(Order.id).where(Order.client_id == client.id))).all())
            if ids:
                await db.execute(delete(OrderFile).where(OrderFile.order_id.in_(ids)))
                await db.execute(delete(Order).where(Order.id.in_(ids)))
            await db.execute(delete(AssistantMaterial).where(AssistantMaterial.source_user_id == uid))
            await db.delete(client)
            await db.commit()
    await state.clear()
    await message.answer("Данные профиля и заявки удалены из базы. Telegram-вложения физически не копируются в хранилище на этом этапе.",
                         reply_markup=ReplyKeyboardRemove())


# ---------- Кнопки меню ----------

@router.message(F.text == "Заказать карточку")
async def begin(message: Message, state: FSMContext):
    await state.clear()
    await ask(message, Brief.platform, state)


@router.message(F.text == "Примеры работ")
async def examples(message: Message):
    folder = BASE_DIR / "content" / "examples"
    files = sorted(p for p in folder.glob("*") if p.suffix.lower() in logic.ALLOWED_EXT) if folder.exists() else []
    if not files:
        await message.answer("Примеры пока не добавлены.")
        return
    for file in files[:10]:
        try:
            await message.answer_photo(FSInputFile(file), caption=file.stem)
        except TelegramAPIError:
            log.exception("Не удалось отправить пример %s", file.name)


@router.message(F.text == "Цены")
async def prices(message: Message):
    try:
        data = yaml.safe_load((BASE_DIR / "config" / "price.yaml").read_text(encoding="utf-8")) or {}
        currency = data.get("currency", "RUB")
        lines = [f"• {x['name']}: {x['price']} {currency}" for x in data.get("items") or []
                 if isinstance(x, dict) and x.get("name") and x.get("price") not in (None, "")]
    except Exception:
        log.exception("Не удалось прочитать config/price.yaml")
        lines = []
    await message.answer("\n".join(lines) if lines else "Прайс ещё не заполнен. Уточни стоимость у администратора.")


@router.message(F.text == "Связаться с автором")
async def contact_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(Contact.message)
    await state.update_data(contact_messages=[])
    await message.answer("Напиши сообщение. Можно несколькими сообщениями. Когда закончишь, нажми «Готово».",
                         reply_markup=cancel_menu())


@router.message(F.forward_origin, StateFilter(None), lambda m: is_admin(m.from_user))
async def collect_forward(message: Message):
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


# ---------- Бриф ----------

@router.message(Brief.platform)
async def platform_step(message: Message, state: FSMContext):
    platform = logic.parse_platform(message.text)
    if platform is None:
        await message.answer("Выбери площадку кнопкой: WB, Ozon или Avito.")
        return
    await state.update_data(platform=platform)
    await ask(message, Brief.product, state)


@router.message(Brief.product, F.text)
@router.message(Brief.advantages, F.text)
@router.message(Brief.required_text, F.text)
@router.message(Brief.style, F.text)
@router.message(Brief.colors, F.text)
async def text_step(message: Message, state: FSMContext):
    cur = await state.get_state()
    st = next(s for s in TEXT_KEYS if s.state == cur)
    key, limit = TEXT_KEYS[st]
    value = logic.clip(message.text, limit)
    if not value:
        await message.answer("Ответ не должен быть пустым.")
        return
    await state.update_data(**{key: value})
    idx = ORDER.index(st)
    await ask(message, ORDER[idx + 1], state)


@router.message(Brief.slides)
async def slides_step(message: Message, state: FSMContext):
    count = logic.parse_slides(message.text)
    if count is None:
        await message.answer("Укажи целое число от 1 до 20.")
        return
    await state.update_data(slides=count)
    await ask(message, Brief.deadline, state)


@router.message(Brief.deadline, F.text)
async def deadline_step(message: Message, state: FSMContext):
    await state.update_data(deadline=logic.clip(message.text, 200), files=[], reference_texts=[])
    await ask(message, Brief.materials, state)


@router.message(Brief.materials, F.text.casefold() == "готово")
async def brief_done(message: Message, state: FSMContext):
    async with user_lock(message.from_user.id):
        data = await state.get_data()
        files = data.get("files", [])
        refs = data.get("reference_texts", [])
        if not files and not refs:
            await message.answer("Пришли хотя бы одно фото товара, ссылку или описание референса, затем нажми «Готово».")
            return
        summary = (
            f"Площадка: {data.get('platform')}\nТовар: {data.get('product')}\n"
            f"Преимущества: {data.get('advantages')}\nТексты: {data.get('required_text')}\n"
            f"Стиль: {data.get('style')}\nЦвета: {data.get('colors')}\n"
            f"Слайдов: {data.get('slides')}\nСрок: {data.get('deadline')}\n"
            f"Файлов: {len(files)}\nРеференсов: {len(refs)}"
        )
        await state.update_data(summary=summary)
        await state.set_state(Brief.confirmation)
    await message.answer("Проверь бриф:\n\n" + summary, reply_markup=ReplyKeyboardRemove())
    await message.answer("Всё верно?", reply_markup=confirm_menu())


@router.message(Brief.materials)
async def material_step(message: Message, state: FSMContext):
    file_id = kind = filename = None
    if message.photo:
        photo = message.photo[-1]
        err = logic.check_photo_size(photo.file_size, settings.max_file_mb)
        if err:
            await message.answer(err)
            return
        file_id, kind = photo.file_id, "photo"
    elif message.document:
        doc = message.document
        err = logic.check_image_document(doc.mime_type, doc.file_name, doc.file_size, settings.max_file_mb)
        if err:
            await message.answer(err)
            return
        file_id, kind, filename = doc.file_id, "document", doc.file_name
    elif message.text:
        async with user_lock(message.from_user.id):
            data = await state.get_data()
            refs = list(data.get("reference_texts", []))
            if len(refs) >= 20:
                await message.answer("Достаточно референсов (20). Нажми «Готово».")
                return
            refs.append(logic.clip(message.text, 2000))
            await state.update_data(reference_texts=refs)
        await message.answer("Ссылка или описание сохранены. Пришли ещё или нажми «Готово».")
        return
    else:
        await message.answer("Этот тип сообщения не поддерживается. Пришли изображение, ссылку или нажми «Готово».")
        return
    async with user_lock(message.from_user.id):
        data = await state.get_data()
        files = list(data.get("files", []))
        if len(files) >= settings.max_files_per_order:
            await message.answer(f"Достигнут лимит: {settings.max_files_per_order} файлов. Нажми «Готово».")
            return
        files.append({"file_id": file_id, "type": kind, "name": filename})
        refs = list(data.get("reference_texts", []))
        if message.caption and len(refs) < 20:
            refs.append(logic.clip(message.caption, 2000))
        await state.update_data(files=files, reference_texts=refs)
    await message.answer(f"Материал принят ({len(files)}/{settings.max_files_per_order}). Пришли следующий или нажми «Готово».")


@router.message(Brief.confirmation)
async def confirmation_text(message: Message):
    await message.answer("Нажми «Подтвердить бриф» под сообщением выше, либо «Отмена» / «Назад».", reply_markup=step_menu())


@router.callback_query(F.data == "brief:confirm", Brief.confirmation)
async def confirm_brief(call: CallbackQuery, state: FSMContext):
    async with user_lock(call.from_user.id):
        # повторная проверка внутри блокировки: защита от двойного нажатия
        if await state.get_state() != Brief.confirmation.state:
            await call.answer("Заявка уже оформлена.")
            return
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
    log.info("Новая заявка %s от пользователя %s, файлов: %d", order_id[:8], call.from_user.id, len(data.get("files", [])))
    await call.answer()
    text_ok = f"Бриф принят. Номер заявки: {order_id[:8]}. Владелец студии проверит материалы и свяжется с тобой."
    if call.message:
        try:
            await call.message.edit_text(text_ok)
        except TelegramAPIError:
            await call.message.answer(text_ok)
        await call.message.answer("Главное меню:", reply_markup=menu())
    refs = data.get("reference_texts", [])
    ref_text = ("\n\nРеференсы:\n" + "\n".join(f"- {r[:300]}" for r in refs[:20])) if refs else ""
    await notify_admin(call.bot, f"Новая заявка {order_id}\nКлиент: @{call.from_user.username or 'без username'} "
                                 f"({call.from_user.id})\n\n{data.get('summary', '')}{ref_text}")
    for item in data.get("files", []):
        try:
            if item["type"] == "photo":
                await call.bot.send_photo(settings.admin_id, item["file_id"])
            else:
                await call.bot.send_document(settings.admin_id, item["file_id"])
        except TelegramAPIError:
            log.exception("Не удалось переслать файл администратору (заявка %s)", order_id[:8])


@router.callback_query(F.data == "brief:restart")
async def restart_brief(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(Brief.platform)
    await call.answer()
    if call.message:
        await call.message.answer("Начнём бриф заново. " + PLATFORM_Q, reply_markup=platform_menu())


@router.callback_query()
async def stale_callback(call: CallbackQuery):
    await call.answer("Кнопка устарела. Начни заново через /start.", show_alert=True)


# ---------- Обращение к автору ----------

@router.message(Contact.message, F.text.casefold() == "готово")
async def contact_done(message: Message, state: FSMContext):
    data = await state.get_data()
    body = "\n".join(data.get("contact_messages", []))
    await state.clear()
    if not body:
        await message.answer("Пустое обращение отменено.", reply_markup=menu())
        return
    sent = await notify_admin(message.bot, f"Обращение от {message.from_user.id} "
                                           f"(@{message.from_user.username or 'нет username'}):\n\n{body}")
    await message.answer("Сообщение передано владельцу студии." if sent else
                         "Не удалось передать сообщение. Попробуй позже.", reply_markup=menu())


@router.message(Contact.message)
async def contact_collect(message: Message, state: FSMContext):
    async with user_lock(message.from_user.id):
        data = await state.get_data()
        items = list(data.get("contact_messages", []))
        if len(items) >= 10:
            await message.answer("Достаточно сообщений. Нажми «Готово».")
            return
        items.append((message.text or message.caption or "[вложение — не передаётся]")[:3000])
        await state.update_data(contact_messages=items)
    await message.answer("Записал. Добавь ещё или нажми «Готово».")


@router.message()
async def fallback(message: Message, state: FSMContext):
    if await state.get_state() is not None:
        await message.answer("Не понял сообщение. Ответь на вопрос выше, либо «Назад» / «Отмена».")
        return
    await message.answer("Не понял. Выбери действие в меню или напиши /start.", reply_markup=menu())


# ---------- Ошибки и запуск ----------

async def on_error(event: ErrorEvent) -> bool:
    log.error("Необработанная ошибка в обработчике (update_id=%s)", event.update.update_id,
              exc_info=event.exception)
    msg = event.update.message or (event.update.callback_query.message if event.update.callback_query else None)
    if msg is not None:
        try:
            await msg.answer("Что-то пошло не так. Попробуй ещё раз или напиши /start.")
        except Exception:
            pass
    return True


async def init_db(max_attempts: int = 6) -> None:
    for attempt in range(1, max_attempts + 1):
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await conn.execute(text("SELECT 1"))
            return
        except Exception as exc:
            log.error("БД недоступна (попытка %d/%d): %s", attempt, max_attempts, type(exc).__name__)
            if attempt == max_attempts:
                raise
            await asyncio.sleep(min(2 ** attempt, 15))


async def main() -> int:
    global settings, engine, Session
    setup_logging()
    log.info("Запуск бота (Python %s)", sys.version.split()[0])
    try:
        settings = Settings()
    except ValidationError as exc:
        for err in exc.errors():
            log.critical("Ошибка конфигурации: переменная %s — %s", ".".join(map(str, err["loc"])).upper(), err["msg"])
        return 2
    if settings.admin_id <= 0:
        log.critical("ADMIN_ID должен быть положительным числом (Telegram ID)")
        return 2
    log.info("Конфигурация: admin_id задан, storage=%s, max_file_mb=%s, max_files=%s, image_provider=%s, "
             "llm_provider=%s, lead_radar=%s", settings.storage_path, settings.max_file_mb,
             settings.max_files_per_order, settings.image_provider or "-", settings.llm_provider or "-",
             settings.enable_lead_radar)

    ok, info = check_storage(settings.storage_path)
    if ok:
        (settings.storage_path / "orders").mkdir(exist_ok=True)
        log.info("Хранилище доступно для записи: %s", settings.storage_path)
    else:
        log.error("Хранилище НЕдоступно: %s (бот продолжит работу — файлы пока не копируются)", info)

    url = make_url(settings.db_url)
    log.info("База данных: %s", url.render_as_string(hide_password=True))
    engine = create_async_engine(settings.db_url, pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    bot = Bot(settings.bot_token)
    try:
        await init_db()
        log.info("БД подключена, таблицы проверены/созданы")
        me = await bot.get_me()
        log.info("Бот авторизован: @%s (id=%s)", me.username, me.id)
        await bot.delete_webhook(drop_pending_updates=False)
        dp = Dispatcher(storage=DatabaseStorage())
        dp.include_router(router)
        dp.errors.register(on_error)
        log.info("Запускаю polling")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), handle_signals=True)
        log.info("Polling остановлен (получен сигнал завершения)")
        return 0
    except Exception:
        log.critical("Фатальная ошибка при запуске/работе", exc_info=True)
        return 1
    finally:
        await bot.session.close()
        await engine.dispose()
        log.info("Ресурсы освобождены, выход")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
