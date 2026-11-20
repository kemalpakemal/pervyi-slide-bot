from __future__ import annotations

import asyncio
import base64
import email.utils
import mimetypes
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Sequence

import httpx


class ImageGenerationError(RuntimeError):
    """Safe, user-facing image generation failure. Never contains request secrets."""
    def __init__(self, message: str, *, uncertain: bool = False, request_succeeded: bool = False):
        super().__init__(message)
        self.uncertain = uncertain
        self.request_succeeded = request_succeeded


RETRYABLE_STATUSES = {502, 503, 504}


def build_product_prompt(brief: dict, slide_number: int, total_slides: int) -> str:
    def clean(key: str, limit: int = 900) -> str:
        value = brief.get(key, "")
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(item) for item in value)
        return " ".join(str(value or "").split())[:limit] or "не указано"

    product = clean("product")
    platform = clean("platform")
    advantages = clean("advantages")
    required_text = clean("required_text")
    style = clean("style")
    colors = clean("colors")
    references = clean("reference_texts", 1200)
    slide_role = (
        "обложка товара: главный оффер и визуальная иерархия"
        if slide_number == 1
        else f"инфографический слайд {slide_number}: раскрой преимущество без повторения обложки"
    )
    return (
        f"Создай вертикальную коммерческую карточку товара для площадки {platform}. "
        f"Товар: {product}. Слайд {slide_number} из {total_slides}, задача: {slide_role}. "
        f"Преимущества из брифа: {advantages}. Обязательные надписи и факты: {required_text}. "
        f"Стиль: {style}. Цветовые пожелания: {colors}. "
        f"Текстовые описания референсов: {references}. "
        "Первое входное изображение — единственный источник истины о товаре; остальные изображения "
        "используй только как референсы композиции и стиля. Сохрани без изменений геометрию, форму, "
        "пропорции, конструкцию, оттенок и фактуру товара, его комплектность, логотип, этикетку, "
        "упаковку и все видимые надписи на самом товаре. Не перерисовывай, не переводи и не выдумывай "
        "текст на этикетке; не подменяй модель и не добавляй отсутствующие детали или характеристики. "
        "Товар должен оставаться узнаваемым и физически достоверным. Создай аккуратную премиальную "
        "инфографику маркетплейса с хорошей читаемостью, чистой композицией и свободными полями. "
        "Тексты карточки размещай отдельно от этикетки товара. Не добавляй водяные знаки, цены, "
        "рейтинги, сертификаты или неподтверждённые обещания. Верни только готовое изображение."
    )


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except (TypeError, ValueError):
        try:
            parsed = email.utils.parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


async def generate_two_images(
    *,
    api_key: str,
    base_url: str,
    model: str,
    prompt: str,
    image_paths: Sequence[Path],
    output_paths: Sequence[Path],
    idempotency_key: str,
    timeout_seconds: float = 180.0,
    max_attempts: int = 3,
    size: str = "1024x1536",
    quality: str = "medium",
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    jitter: Callable[[float, float], float] = random.uniform,
) -> list[Path]:
    """Edit using product + reference images; retry transient failures with a stable idempotency key."""
    if not api_key:
        raise ImageGenerationError("Ключ API не настроен.")
    if len(output_paths) != 2:
        raise ValueError("Exactly two output paths are required.")
    if not image_paths:
        raise ImageGenerationError("Нет исходного фото товара.")
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")

    endpoint = base_url.rstrip("/") + "/images/edits"
    timeout = httpx.Timeout(timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        for attempt in range(1, max_attempts + 1):
            files = []
            opened = []
            try:
                for path in image_paths:
                    handle = path.open("rb")
                    opened.append(handle)
                    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                    files.append(("image[]", (path.name, handle, mime)))
                response = await client.post(
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Idempotency-Key": idempotency_key,
                        "Accept": "application/json",
                    },
                    data={
                        "model": model,
                        "prompt": prompt,
                        "n": "2",
                        "size": size,
                        "quality": quality,
                    },
                    files=files,
                )
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt >= max_attempts:
                    raise ImageGenerationError("Сеть или таймаут генератора после трёх попыток.", uncertain=True)
                delay = min(30.0, 2 ** (attempt - 1) + jitter(0.0, 0.5))
                await sleep(delay)
                continue
            finally:
                for handle in opened:
                    handle.close()

            if response.status_code in RETRYABLE_STATUSES:
                if attempt >= max_attempts:
                    raise ImageGenerationError(
                        f"Генератор временно недоступен (HTTP {response.status_code}) после трёх попыток.",
                        uncertain=True,
                    )
                backoff = min(30.0, 2 ** (attempt - 1) + jitter(0.0, 0.5))
                retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                if retry_after is not None and retry_after > 300:
                    raise ImageGenerationError("Генератор запросил слишком длинную паузу; переключаю заказ в ручной режим.", uncertain=True)
                await sleep(max(backoff, retry_after or 0.0))
                continue

            if response.status_code >= 400:
                # Never echo provider error bodies: they may contain account or credential details.
                if response.status_code in {401, 403}:
                    raise ImageGenerationError("Генератор отклонил авторизацию или доступ к модели.")
                if response.status_code == 402:
                    raise ImageGenerationError("Недостаточно средств или исчерпан баланс API.")
                if response.status_code == 429:
                    try:
                        error_payload = response.json()
                        error_code = str((error_payload.get("error") or {}).get("code") or "").lower()
                    except (ValueError, TypeError, AttributeError):
                        error_code = ""
                    if error_code in {"credit_balance_exhausted", "billing_hard_limit_reached", "insufficient_quota"}:
                        raise ImageGenerationError("Баланс или лимит расходов API исчерпан; повтор не выполнялся.")
                    raise ImageGenerationError("Превышен лимит запросов API; повтор не выполнялся.")
                raise ImageGenerationError(
                    f"Генератор отклонил запрос (HTTP {response.status_code}); повтор не выполнялся."
                )

            try:
                payload = response.json()
                rows = payload.get("data") or []
                encoded = [row.get("b64_json") for row in rows[:2]]
                if len(encoded) != 2 or not all(encoded):
                    raise ValueError("missing image data")
                decoded = [base64.b64decode(item, validate=True) for item in encoded]
                for path, content in zip(output_paths, decoded):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temp_path = path.with_suffix(path.suffix + ".tmp")
                    temp_path.write_bytes(content)
                    temp_path.replace(path)
            except (ValueError, TypeError, KeyError):
                raise ImageGenerationError(
                    "Генератор вернул некорректный ответ с изображениями.",
                    uncertain=True,
                    request_succeeded=True,
                )
            except OSError:
                raise ImageGenerationError(
                    "Не удалось сохранить результат генератора.",
                    uncertain=True,
                    request_succeeded=True,
                )

            return list(output_paths)

    raise ImageGenerationError("Генерация не завершилась.")
