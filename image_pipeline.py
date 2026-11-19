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
