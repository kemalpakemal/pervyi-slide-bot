"""Чистая логика без Telegram/БД — легко тестируется."""
from __future__ import annotations

PLATFORMS = ("WB", "Ozon", "Avito")
ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp"}
CANCEL_WORDS = {"отмена", "/cancel"}
BACK_WORD = "Назад"


def normalize_db_url(url: str) -> str:
    """Приводит DATABASE_URL Railway/Heroku-формата к async-драйверам SQLAlchemy."""
    url = (url or "").strip()
    for prefix in ("postgres://", "postgresql://", "postgresql+psycopg2://", "postgresql+psycopg://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix):]
    if url.startswith("sqlite://") and not url.startswith("sqlite+aiosqlite://"):
        return "sqlite+aiosqlite://" + url[len("sqlite://"):]
    return url


def parse_platform(text: str | None) -> str | None:
    if not text:
        return None
    t = text.strip().lower()
    mapping = {"wb": "WB", "wildberries": "WB", "вб": "WB", "ozon": "Ozon", "озон": "Ozon", "avito": "Avito", "авито": "Avito"}
    return mapping.get(t)


def parse_slides(text: str | None, lo: int = 1, hi: int = 20) -> int | None:
    if not text:
        return None
    try:
        n = int(text.strip())
    except ValueError:
        return None
    return n if lo <= n <= hi else None


def is_cancel(text: str | None) -> bool:
    return bool(text) and text.strip().lower() in CANCEL_WORDS


def check_image_document(mime: str | None, name: str | None, size: int | None, max_mb: int) -> str | None:
    """Возвращает текст ошибки или None, если файл допустим."""
    if size and size > max_mb * 1024 * 1024:
        return f"Файл больше лимита {max_mb} МБ."
    ext = ("." + name.rsplit(".", 1)[1].lower()) if name and "." in name else ""
    if mime not in ALLOWED_MIME and not (mime is None and ext in ALLOWED_EXT):
        return "Принимаю только изображения JPG, PNG и WEBP."
    return None


def check_photo_size(size: int | None, max_mb: int) -> str | None:
    if size and size > max_mb * 1024 * 1024:
        return f"Файл больше лимита {max_mb} МБ."
    return None


def clip(text: str | None, limit: int) -> str:
    return (text or "").strip()[:limit]
