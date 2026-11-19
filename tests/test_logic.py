import pytest

import logic


@pytest.mark.parametrize("src,expected", [
    ("postgres://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db"),
    ("postgresql://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
    ("postgresql+asyncpg://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
    ("postgresql+psycopg2://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
    ("sqlite:///./a.db", "sqlite+aiosqlite:///./a.db"),
    ("sqlite+aiosqlite:///./a.db", "sqlite+aiosqlite:///./a.db"),
    ("  postgres://u@h/db \n", "postgresql+asyncpg://u@h/db"),
])
def test_normalize_db_url(src, expected):
    assert logic.normalize_db_url(src) == expected


@pytest.mark.parametrize("src,expected", [
    ("WB", "WB"), (" wb ", "WB"), ("Ozon", "Ozon"), ("озон", "Ozon"), ("Авито", "Avito"),
    ("Яндекс", None), ("", None), (None, None),
])
def test_parse_platform(src, expected):
    assert logic.parse_platform(src) == expected


@pytest.mark.parametrize("src,expected", [
    ("5", 5), (" 20 ", 20), ("1", 1), ("0", None), ("21", None), ("-3", None),
    ("abc", None), ("2.5", None), ("", None), (None, None),
])
def test_parse_slides(src, expected):
    assert logic.parse_slides(src) == expected


def test_check_image_document():
    mb = 1024 * 1024
    assert logic.check_image_document("image/png", "a.png", mb, 15) is None
    assert logic.check_image_document("image/jpeg", None, None, 15) is None
    assert logic.check_image_document(None, "a.WEBP", 10, 15) is None
    assert "лимита 15" in logic.check_image_document("image/png", "a.png", 16 * mb, 15)
    assert logic.check_image_document("application/pdf", "a.pdf", 10, 15)
    assert logic.check_image_document("image/gif", "a.gif", 10, 15)
    assert logic.check_image_document(None, "a.exe", 10, 15)


def test_check_photo_size():
    assert logic.check_photo_size(None, 15) is None
    assert logic.check_photo_size(20 * 1024 * 1024, 15)


def test_is_cancel_and_clip():
    assert logic.is_cancel(" Отмена ") and logic.is_cancel("/cancel")
    assert not logic.is_cancel("привет") and not logic.is_cancel(None)
    assert logic.clip("  abcdef ", 3) == "abc"
    assert logic.clip(None, 3) == ""
