from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


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


def apply_text_overlay(
    source_path: Path,
    destination: Path,
    title: str = "",
    benefits: list[str] | None = None,
) -> Path:
    """Optional factual text layer. Product pixels remain untouched."""
    image = Image.open(source_path).convert("RGBA")
    width, height = image.size
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")
    pad = max(24, width // 24)
    panel_h = max(150, height // 4)
    draw.rounded_rectangle(
        (pad, pad, width - pad, pad + panel_h),
        radius=max(18, width // 35),
        fill=(0, 0, 0, 145),
        outline=(255, 255, 255, 95),
        width=max(1, width // 500),
    )
    title_font = _font(max(24, width // 18), bold=True)
    body_font = _font(max(17, width // 30), bold=False)
    y = pad + max(16, panel_h // 10)
    if title.strip():
        draw.text((pad * 1.5, y), title.strip()[:80], font=title_font, fill=(255, 255, 255, 245))
        y += int(title_font.size * 1.35)
    for benefit in (benefits or [])[:3]:
        if y > pad + panel_h - body_font.size:
            break
        draw.text((pad * 1.5, y), f"• {benefit[:70]}", font=body_font, fill=(255, 255, 255, 235))
        y += int(body_font.size * 1.35)
    result = Image.alpha_composite(image, overlay).convert("RGB")
    destination.parent.mkdir(parents=True, exist_ok=True)
    result.save(destination, format="PNG", optimize=True)
    return destination


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
    font = _font(max(17, width // 24), bold=True)
    # Repeated rotated text stamps overlap so the watermark covers the whole preview.
    stamp_w = max(420, width * 2 // 3)
    stamp_h = max(52, font.size * 2)
    stamp = Image.new("RGBA", (stamp_w, stamp_h), (0, 0, 0, 0))
    stamp_draw = ImageDraw.Draw(stamp)
    stamp_draw.text((8, 4), watermark_text, font=font, fill=(255, 255, 255, 112),
                    stroke_width=1, stroke_fill=(0, 0, 0, 72))
    stamp_draw.text((8, font.size), watermark_text, font=font, fill=(255, 255, 255, 82),
                    stroke_width=1, stroke_fill=(0, 0, 0, 60))
    rotated = stamp.rotate(28, resample=Image.Resampling.BICUBIC, expand=True)
    step_x = max(90, rotated.width // 3)
    step_y = max(75, rotated.height // 2)
    for y in range(-rotated.height, height + rotated.height, step_y):
        for x in range(-rotated.width, width + rotated.width, step_x):
            overlay.paste(rotated, (x, y), rotated)

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
        fill=(0, 0, 0, 115),
        outline=(255, 255, 255, 135),
        width=2,
    )
    draw.text((x, y), label, font=label_font, fill=(255, 255, 255, 220),
              stroke_width=2, stroke_fill=(0, 0, 0, 190))
    result = Image.alpha_composite(image, label_overlay).convert("RGB")
    destination.parent.mkdir(parents=True, exist_ok=True)
    result.save(destination, format="JPEG", quality=90, optimize=True)
    return destination
