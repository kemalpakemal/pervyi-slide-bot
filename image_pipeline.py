from __future__ import annotations

import warnings
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps


SUPPORTED_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP"}
MAX_IMAGE_PIXELS = 40_000_000


class ImagePipelineError(RuntimeError):
    pass


def validate_image_file(source_path: Path, max_bytes: int | None = None) -> tuple[str, tuple[int, int]]:
    """Validate actual image content; never trust only the extension or Telegram MIME type."""
    try:
        if not source_path.is_file():
            raise ImagePipelineError("Файл изображения не найден.")
        size_bytes = source_path.stat().st_size
        if size_bytes <= 0:
            raise ImagePipelineError("Файл изображения пуст.")
        if max_bytes is not None and size_bytes > max_bytes:
            raise ImagePipelineError("Размер изображения превышает допустимый лимит.")

        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source_path) as image:
                image_format = (image.format or "").upper()
                dimensions = image.size
                if image_format not in SUPPORTED_IMAGE_FORMATS:
                    raise ImagePipelineError("Поддерживаются только JPEG, PNG и WEBP.")
                if len(dimensions) != 2 or min(dimensions) < 32:
                    raise ImagePipelineError("Слишком маленькое изображение.")
                if dimensions[0] * dimensions[1] > MAX_IMAGE_PIXELS:
                    raise ImagePipelineError("Слишком большое разрешение изображения.")
                image.verify()

            # Reopen after verify, apply EXIF orientation and force full pixel decoding.
            with Image.open(source_path) as image:
                oriented = ImageOps.exif_transpose(image)
                oriented.load()
                dimensions = oriented.size
                if dimensions[0] * dimensions[1] > MAX_IMAGE_PIXELS:
                    raise ImagePipelineError("Слишком большое разрешение изображения.")

        return image_format, dimensions
    except ImagePipelineError:
        raise
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImagePipelineError("Файл повреждён или не является корректным изображением.") from exc


def _load_oriented_rgba(source_path: Path) -> Image.Image:
    validate_image_file(source_path)
    try:
        with Image.open(source_path) as source:
            image = ImageOps.exif_transpose(source)
            image.load()
            return image.convert("RGBA")
    except (OSError, ValueError, SyntaxError) as exc:
        raise ImagePipelineError("Не удалось открыть изображение.") from exc


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


def _wrap_text(draw: ImageDraw.ImageDraw, value: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    words = " ".join(str(value or "").split()).split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        if not word:
            continue
        candidate = f"{current} {word}".strip()
        if current and draw.textlength(candidate, font=font) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def apply_text_overlay(
    source_path: Path,
    destination: Path,
    title: str = "",
    benefits: list[str] | None = None,
) -> Path:
    """Optional factual text layer; product pixels and source transparency are preserved."""
    image = _load_oriented_rgba(source_path)
    width, height = image.size
    if width < 64 or height < 64:
        raise ImagePipelineError("Недостаточное разрешение для текстовой плашки.")

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")
    pad = max(16, width // 24)
    inner_width = max(32, width - 3 * pad)
    title_font = _font(max(20, width // 18), bold=True)
    body_font = _font(max(15, width // 30), bold=False)

    title_lines = _wrap_text(draw, title.strip()[:120], title_font, inner_width)[:2] if title.strip() else []
    benefit_lines: list[str] = []
    for benefit in (benefits or [])[:3]:
        benefit_lines.extend(_wrap_text(draw, str(benefit)[:100], body_font, inner_width)[:2])

    line_gap = max(4, width // 180)
    title_step = max(title_font.size + line_gap, 1)
    body_step = max(body_font.size + line_gap, 1)
    content_height = len(title_lines) * title_step + len(benefit_lines) * body_step
    panel_height = min(height - 2 * pad, max(96, content_height + 2 * pad))
    if panel_height <= 0:
        raise ImagePipelineError("Недостаточно места для текстовой плашки.")

    draw.rounded_rectangle(
        (pad, pad, width - pad, pad + panel_height),
        radius=max(12, width // 35),
        fill=(0, 0, 0, 178),
        outline=(255, 255, 255, 190),
        width=max(1, width // 500),
    )
    y = pad + max(10, panel_height // 10)
    x = pad + max(12, pad // 2)
    for line in title_lines:
        draw.text((x, y), line, font=title_font, fill=(255, 255, 255, 255),
                  stroke_width=1, stroke_fill=(0, 0, 0, 210))
        y += title_step
    for line in benefit_lines:
        if y + body_font.size > pad + panel_height - 8:
            break
        draw.text((x, y), f"• {line}", font=body_font, fill=(255, 255, 255, 250),
                  stroke_width=1, stroke_fill=(0, 0, 0, 200))
        y += body_step

    result = Image.alpha_composite(image, overlay)
    destination.parent.mkdir(parents=True, exist_ok=True)
    result.save(destination, format="PNG", optimize=True)
    return destination


def make_watermarked_demo(
    source_path: Path,
    destination: Path,
    max_long_edge: int = 700,
    watermark_text: str = "ПЕРВЫЙ СЛАЙД • ДЕМО",
) -> Path:
    if max_long_edge < 64:
        raise ImagePipelineError("Размер демо должен быть не меньше 64 пикселей.")
    image = _load_oriented_rgba(source_path)

    # Transparent product cut-outs need an explicit background before JPEG export.
    background = Image.new("RGBA", image.size, (255, 255, 255, 255))
    background.alpha_composite(image)
    image = background

    scale = min(1.0, max_long_edge / max(image.size))
    if scale < 1:
        image = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            Image.Resampling.LANCZOS,
        )

    width, height = image.size
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    font = _font(max(17, width // 24), bold=True)
    label_text = (watermark_text or "ПЕРВЫЙ СЛАЙД • ДЕМО")[:80]
    stamp_w = max(420, width * 2 // 3)
    stamp_h = max(60, font.size * 2 + 12)
    stamp = Image.new("RGBA", (stamp_w, stamp_h), (0, 0, 0, 0))
    stamp_draw = ImageDraw.Draw(stamp)
    stamp_draw.text(
        (8, 4), label_text, font=font, fill=(255, 255, 255, 150),
        stroke_width=max(2, width // 350), stroke_fill=(0, 0, 0, 210),
    )
    stamp_draw.text(
        (8, font.size), label_text, font=font, fill=(255, 255, 255, 125),
        stroke_width=max(2, width // 350), stroke_fill=(0, 0, 0, 190),
    )
    rotated = stamp.rotate(28, resample=Image.Resampling.BICUBIC, expand=True)
    step_x = max(90, rotated.width // 3)
    step_y = max(75, rotated.height // 2)
    for y in range(-rotated.height, height + rotated.height, step_y):
        for x in range(-rotated.width, width + rotated.width, step_x):
            overlay.alpha_composite(rotated, (x, y))

    image = Image.alpha_composite(image, overlay)
    label_overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(label_overlay, "RGBA")
    label_font = _font(max(38, width // 8), bold=True)
    label = "ДЕМО"
    box = draw.textbbox((0, 0), label, font=label_font, stroke_width=2)
    label_w, label_h = box[2] - box[0], box[3] - box[1]
    x = (width - label_w) // 2
    y = (height - label_h) // 2
    pad = max(16, width // 35)
    draw.rounded_rectangle(
        (x - pad, y - pad, x + label_w + pad, y + label_h + pad),
        radius=18,
        fill=(0, 0, 0, 220),
        outline=(255, 255, 255, 245),
        width=max(2, width // 350),
    )
    draw.text((x, y), label, font=label_font, fill=(255, 255, 255, 255),
              stroke_width=2, stroke_fill=(0, 0, 0, 255))

    result = Image.alpha_composite(image, label_overlay).convert("RGB")
    destination.parent.mkdir(parents=True, exist_ok=True)
    result.save(destination, format="JPEG", quality=92, optimize=True)
    return destination
