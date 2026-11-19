from __future__ import annotations

import base64
import io
import re
import textwrap
from pathlib import Path
from typing import Iterable

import httpx
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps


class ImagePipelineError(RuntimeError):
    pass


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"]
        if bold else
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"]
    )
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


class OctavApiImageProvider:
    """Image generation client for OctavApi's documented Images endpoint."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://octavapi.shop/v1",
        model: str = "image_gen",
        size: str = "1024x1024",
    ):
        if not api_key.strip():
            raise ImagePipelineError(
                "Не задан OCTAVAPI_API_KEY. Добавь ключ OctavApi в Railway → Variables."
            )
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.model = model.strip()
        self.size = size.strip()

    async def generate(
        self,
        prompt: str,
        destination: Path,
        safety_identifier: str | None = None,
    ) -> Path:
        prompt = " ".join(prompt.split())
        if not prompt:
            raise ImagePipelineError("Пустое описание фона.")

        url = f"{self.base_url}/images/generations"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "prompt": prompt,
            "size": self.size,
        }

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(240.0, connect=30.0),
                follow_redirects=True,
            ) as client:
                response = await client.post(url, headers=headers, json=payload)
                if response.is_error:
                    try:
                        parsed_error = response.json()
                        detail = (
                            parsed_error.get("error", {}).get("message")
                            if isinstance(parsed_error.get("error"), dict)
                            else parsed_error.get("error")
                        ) or parsed_error.get("message") or response.text[:900]
                    except (ValueError, AttributeError):
                        detail = response.text[:900]
                    raise ImagePipelineError(
                        f"OctavApi Images: HTTP {response.status_code}: {detail}"
                    )
                body = response.json()

                # Accept only common image response representations. If the
                # gateway returns a different schema, fail with a safe diagnostic.
                data = body.get("data") if isinstance(body, dict) else None
                item = data[0] if isinstance(data, list) and data else None
                if not isinstance(item, dict):
                    item = body.get("output") if isinstance(body, dict) else None
                    if isinstance(item, list) and item and isinstance(item[0], dict):
                        item = item[0]

                encoded = None
                image_url = None
                if isinstance(item, dict):
                    encoded = item.get("b64_json") or item.get("base64")
                    image_url = item.get("url") or item.get("image_url")
                if not encoded and not image_url and isinstance(body, dict):
                    encoded = body.get("b64_json") or body.get("base64")
                    image_url = body.get("url") or body.get("image_url")

                if encoded:
                    try:
                        image_bytes = base64.b64decode(encoded, validate=True)
                    except Exception as exc:
                        raise ImagePipelineError(
                            "OctavApi вернул некорректные base64-данные изображения."
                        ) from exc
                elif image_url:
                    image_response = await client.get(str(image_url))
                    image_response.raise_for_status()
                    image_bytes = image_response.content
                else:
                    keys = ", ".join(sorted(body.keys())) if isinstance(body, dict) else type(body).__name__
                    raise ImagePipelineError(
                        "OctavApi принял запрос, но вернул неизвестный формат ответа. "
                        f"Поля верхнего уровня: {keys}. Нужен пример JSON-ответа из документации."
                    )

                try:
                    with Image.open(io.BytesIO(image_bytes)) as image:
                        image.convert("RGB").save(destination, format="JPEG", quality=94)
                except Exception as exc:
                    raise ImagePipelineError(
                        "OctavApi вернул данные, которые не удалось открыть как изображение."
                    ) from exc

        except ImagePipelineError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise ImagePipelineError(
                f"Ошибка соединения с OctavApi Images: {type(exc).__name__}: {str(exc)[:300]}"
            ) from exc
        return destination

def cutout_product(source_path: Path, destination: Path, model_name: str = "u2netp") -> Path:
    """Remove the background from the seller's original photo; never regenerate the product."""
    try:
        from rembg import new_session, remove
    except ImportError as exc:
        raise ImagePipelineError(
            "Не установлен модуль удаления фона rembg. Проверь сборку Railway."
        ) from exc

    try:
        source = Image.open(source_path).convert("RGBA")
        session = new_session(model_name)
        result = remove(source, session=session)
        if isinstance(result, bytes):
            result = Image.open(io.BytesIO(result)).convert("RGBA")
        else:
            result = result.convert("RGBA")

        alpha = result.getchannel("A")
        bbox = alpha.getbbox()
        if not bbox:
            raise ImagePipelineError(
                "Не удалось выделить товар на фото. Нужен снимок с более чёткими границами."
            )
        result = result.crop(bbox)
        result.save(destination, format="PNG", optimize=True)
        return destination
    except ImagePipelineError:
        raise
    except Exception as exc:
        raise ImagePipelineError(f"Не удалось удалить фон с фото товара: {exc}") from exc


def _benefit_lines(value: str | Iterable[str]) -> list[str]:
    if isinstance(value, str):
        parts = re.split(r"[\n;,]+", value)
    else:
        parts = list(value)
    cleaned = [str(item).strip(" \t\r\n•-") for item in parts]
    return [item for item in cleaned if item][:5]


def build_background_prompt(
    product_name: str,
    style: str,
    colors: str,
    slide_number: int,
) -> str:
    scene = (
        "premium clean studio product photography background, soft sculpted lighting, "
        "subtle depth, uncluttered negative space"
        if slide_number == 1 else
        "modern editorial product photography set, restrained props, clear visual hierarchy, "
        "uncluttered negative space"
    )
    prompt = (
        f"Create a vertical 2:3 background for an e-commerce product infographic about "
        f"{product_name[:60]}. Visual style: {style[:45] or 'modern commercial minimalism'}. "
        f"Palette: {colors[:35] or 'neutral warm tones'}. {scene}. "
        "Background only: do not show the product, any similar object, people, packaging, "
        "text, letters, numbers, logos, labels or watermarks. Leave clear space for typography."
    )
    return " ".join(prompt.split())[:490]


def _wrap_by_pixels(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and draw.textlength(candidate, font=font) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def compose_slide(
    background_path: Path,
    cutout_path: Path,
    destination: Path,
    product_name: str,
    benefits: list[str],
    slide_number: int,
    width: int = 1200,
    height: int = 1600,
) -> Path:
    try:
        background = Image.open(background_path).convert("RGB")
        canvas = ImageOps.fit(background, (width, height), method=Image.Resampling.LANCZOS).convert("RGBA")

        product = Image.open(cutout_path).convert("RGBA")
        product.thumbnail((920, 850), Image.Resampling.LANCZOS)
        alpha = product.getchannel("A")

        # Soft contact shadow, kept separate from the untouched original product pixels.
        shadow_alpha = alpha.filter(ImageFilter.GaussianBlur(28))
        shadow = Image.new("RGBA", product.size, (0, 0, 0, 0))
        shadow.putalpha(shadow_alpha.point(lambda value: int(value * 0.48)))
        x = (width - product.width) // 2
        y = 420 + max(0, (960 - product.height) // 2)
        canvas.alpha_composite(shadow, (x + 16, y + 24))
        canvas.alpha_composite(product, (x, y))

        draw = ImageDraw.Draw(canvas, "RGBA")
        draw.rounded_rectangle(
            (48, 48, width - 48, 360),
            radius=34,
            fill=(8, 14, 22, 142),
            outline=(255, 255, 255, 62),
            width=2,
        )

        title = product_name.strip() or "Товар"
        if slide_number > 1 and benefits:
            title = benefits[min(slide_number - 2, len(benefits) - 1)]
        title_font = _font(68 if len(title) < 24 else 58, bold=True)
        title_lines = _wrap_by_pixels(draw, title, title_font, width - 150)[:2]
        draw.multiline_text(
            (88, 92), "\n".join(title_lines), font=title_font,
            fill=(255, 255, 255, 255), spacing=10, stroke_width=1,
            stroke_fill=(0, 0, 0, 80),
        )

        draw.rounded_rectangle(
            (48, height - 286, width - 48, height - 48),
            radius=30,
            fill=(8, 14, 22, 162),
            outline=(255, 255, 255, 55),
            width=2,
        )
        footer_font = _font(34, bold=True)
        small_font = _font(30)
        draw.text((88, height - 255), "ПРЕИМУЩЕСТВА", font=footer_font, fill=(255, 255, 255, 235))
        selected = benefits[:3] if slide_number == 1 else benefits[1:4] or benefits[:3]
        if not selected:
            selected = ["Дизайн под площадку", "Чёткая подача товара"]
        for index, item in enumerate(selected[:3]):
            y_line = height - 194 + index * 48
            draw.ellipse((91, y_line + 10, 107, y_line + 26), fill=(255, 220, 130, 255))
            available = width - 190
            lines = _wrap_by_pixels(draw, item, small_font, available)[:1]
            draw.text((126, y_line), lines[0], font=small_font, fill=(255, 255, 255, 250))

        canvas.convert("RGB").save(destination, format="PNG", optimize=True)
        return destination
    except ImagePipelineError:
        raise
    except Exception as exc:
        raise ImagePipelineError(f"Не удалось собрать слайд: {exc}") from exc


def make_watermarked_demo(
    source_path: Path,
    destination: Path,
    max_long_edge: int = 700,
    watermark_text: str = "ПЕРВЫЙ СЛАЙД • ДЕМО",
) -> Path:
    image = Image.open(source_path).convert("RGBA")
    scale = min(1.0, max_long_edge / max(image.size))
    if scale < 1:
        image = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            Image.Resampling.LANCZOS,
        )

    width, height = image.size
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    tile_font = _font(max(20, width // 28), bold=True)
    tile = Image.new("RGBA", (max(360, width), 74), (0, 0, 0, 0))
    tile_draw = ImageDraw.Draw(tile)
    tile_draw.text((12, 9), watermark_text, font=tile_font, fill=(255, 255, 255, 112))
    rotated = tile.rotate(28, resample=Image.Resampling.BICUBIC, expand=True)

    for y in range(0, height, 210):
        for x in range(0, width, max(300, width - 90)):
            overlay.alpha_composite(rotated, (x, y))

    image = Image.alpha_composite(image, overlay)
    center_overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(center_overlay, "RGBA")
    label_font = _font(max(42, width // 8), bold=True)
    label = "ДЕМО"
    box = draw.textbbox((0, 0), label, font=label_font, stroke_width=2)
    label_w, label_h = box[2] - box[0], box[3] - box[1]
    x = (width - label_w) // 2
    y = (height - label_h) // 2
    pad = max(16, width // 35)
    draw.rounded_rectangle(
        (x - pad, y - pad, x + label_w + pad, y + label_h + pad),
        radius=18,
        fill=(0, 0, 0, 95),
        outline=(255, 255, 255, 110),
        width=2,
    )
    draw.text(
        (x, y), label, font=label_font,
        fill=(255, 255, 255, 205),
        stroke_width=2, stroke_fill=(0, 0, 0, 180),
    )
    result = Image.alpha_composite(image, center_overlay).convert("RGB")
    result.save(destination, format="JPEG", quality=90, optimize=True)
    return destination
