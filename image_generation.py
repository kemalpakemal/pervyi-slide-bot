"""Генерация слайдов через OpenAI Images API."""
from __future__ import annotations

import asyncio
import base64
import io
from pathlib import Path

import httpx
from PIL import Image


class GenerationError(RuntimeError):
    pass


# Размеры под площадки: (width, height, openai_size)
PLATFORM_SIZES: dict[str, tuple[int, int, str]] = {
    "WB": (900, 1200, "1024x1536"),
    "Ozon": (900, 1200, "1024x1536"),
    "Avito": (1200, 900, "1536x1024"),
}
DEFAULT_SIZE = (900, 1200, "1024x1536")


def platform_size(platform: str | None) -> tuple[int, int, str]:
    return PLATFORM_SIZES.get((platform or "").strip(), DEFAULT_SIZE)


def _clean(value: object, limit: int = 600) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _benefits_list(brief: dict) -> list[str]:
    raw = brief.get("advantages") or ""
    if isinstance(raw, list):
        raw = "\n".join(str(x) for x in raw)
    parts = [p.strip(" \t\r•-") for p in str(raw).replace(";", "\n").replace(",", "\n").split("\n")]
    return [p for p in parts if p][:5]


def build_slide_prompts(brief: dict) -> list[str]:
    """Строит промпты по слайдам из брифа. Первый слайд — обложка, остальные — инфографика."""
    platform = _clean(brief.get("platform"), 20) or "WB"
    product = _clean(brief.get("product"), 400) or "товар"
    advantages = _benefits_list(brief)
    required = _clean(brief.get("required_text"), 500)
    style = _clean(brief.get("style"), 2500) or "современный рекламный стиль для маркетплейса"
    colors = _clean(brief.get("colors"), 200) or "чистый светлый фон, акценты бренда"
    refs = brief.get("reference_texts") or []
    ref_text = ""
    if isinstance(refs, list) and refs:
        ref_text = " Референсы клиента: " + "; ".join(_clean(r, 200) for r in refs[:3]) + "."

    try:
        slides = int(brief.get("slides") or 1)
    except (TypeError, ValueError):
        slides = 1
    slides = max(1, min(slides, 20))

    adv_text = ", ".join(advantages) if advantages else "выгодные стороны товара"
    base = (
        f"Профессиональная карточка товара для {platform} (Wildberries/Ozon/Avito). "
        f"Товар: {product}. Стиль: {style}. Цвета: {colors}. "
        f"Преимущества: {adv_text}."
    )
    if required and required.lower() not in {"на усмотрение студии", "нет"}:
        base += f" Обязательно разместить текст: {required}."
    base += ref_text
    base += (
        " Высокое качество, студийный свет, чистый фон, товар в центре кадра, "
        "без водяных знаков, без логотипов, без искажённого текста, "
        "текст на русском языке крупным читаемым шрифтом, коммерческая инфографика."
    )

    prompts: list[str] = []
    for i in range(1, slides + 1):
        if i == 1:
            prompts.append(
                base
                + " Это ОБЛОЖКА: товар крупным планом по центру, название товара сверху, "
                  "1-2 главных преимущества плашками, яркий привлекающий взгляд дизайн."
            )
        else:
            focus = advantages[i - 2] if len(advantages) >= i - 1 else adv_text
            prompts.append(
                base
                + f" Это слайд {i} инфографики: фокус на преимуществе «{focus}», "
                  "товар сбоку, крупная иконка/пиктограмма, короткий заголовок и 1 строка описания, "
                  "схематично и понятно, не перегружать."
            )
    return prompts


async def _generate_openai(
    prompt: str,
    openai_size: str,
    api_key: str,
    model: str,
    reference_image: Path | None,
    timeout: float = 180.0,
) -> bytes:
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=timeout) as client:
        if reference_image and reference_image.is_file():
            # Edits: используем фото товара как базу, чтобы сохранить вид товара
            with reference_image.open("rb") as f:
                files = {"image": (reference_image.name, f, "image/jpeg")}
                data = {
                    "model": model if model.startswith("gpt-image") else "gpt-image-1",
                    "prompt": prompt
                    + " Сохрани внешний вид товара с референсного фото, помести его на новый рекламный фон карточки.",
                    "size": openai_size,
                    "n": "1",
                }
                resp = await client.post(
                    "https://api.openai.com/v1/images/edits",
                    headers=headers, files=files, data=data,
                )
        else:
            resp = await client.post(
                "https://api.openai.com/v1/images/generations",
                headers=headers,
                json={
                    "model": model,
                    "prompt": prompt,
                    "size": openai_size,
                    "n": 1,
                },
            )
        if resp.status_code != 200:
            raise GenerationError(f"OpenAI API {resp.status_code}: {resp.text[:500]}")
        payload = resp.json()
        try:
            b64 = payload["data"][0]["b64_json"]
        except (KeyError, IndexError, TypeError) as exc:
            raise GenerationError(f"OpenAI вернул неожиданный ответ: {str(payload)[:500]}") from exc
        return base64.b64decode(b64)


def _normalize_to_size(image_bytes: bytes, width: int, height: int) -> bytes:
    """Обрезает/масштабирует результат под точный размер площадки (900x1200 и т.д.)."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    if img.size == (width, height):
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    # center-crop под пропорции, затем resize
    target_ratio = width / height
    src_ratio = img.width / img.height
    if src_ratio > target_ratio:
        new_h = img.height
        new_w = int(new_h * target_ratio)
        left = (img.width - new_w) // 2
        img = img.crop((left, 0, left + new_w, new_h))
    else:
        new_w = img.width
        new_h = int(new_w / target_ratio)
        top = (img.height - new_h) // 2
        img = img.crop((0, top, new_w, top + new_h))
    img = img.resize((width, height), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


async def generate_slide_image(
    prompt: str,
    platform: str | None,
    provider: str = "auto",
    openai_api_key: str | None = None,
    openai_model: str = "gpt-image-1",
    reference_image: Path | None = None,
    seed: int | None = None,
) -> bytes:
    """Генерирует один слайд и возвращает PNG-байты точного размера площадки."""
    width, height, openai_size = platform_size(platform)
    if (provider or "openai").lower() not in {"auto", "openai"}:
        raise GenerationError("Поддерживается только OpenAI Images API")
    if not openai_api_key:
        raise GenerationError("OPENAI_API_KEY не задан")
    raw = await _generate_openai(prompt, openai_size, openai_api_key, openai_model, reference_image)
    return await asyncio.to_thread(_normalize_to_size, raw, width, height)


def save_generated_image(image_bytes: bytes, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(image_bytes)
    return destination
