import tempfile
import unittest
from pathlib import Path

from PIL import Image

from image_pipeline import (
    ImagePipelineError,
    apply_text_overlay,
    make_watermarked_demo,
    validate_image_file,
)


class ImagePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.png"
        Image.new("RGB", (900, 1200), (110, 130, 150)).save(self.source)

    def tearDown(self):
        self.temp.cleanup()

    def test_demo_is_resized_and_marked(self):
        output = self.root / "demo.jpg"
        make_watermarked_demo(self.source, output, 700, "TEST WATERMARK")
        self.assertTrue(output.is_file())
        with Image.open(output) as result:
            self.assertLessEqual(max(result.size), 700)
            self.assertEqual(result.format, "JPEG")
            colors = result.convert("RGB").getcolors(maxcolors=1000000)
            self.assertIsNotNone(colors)
            self.assertGreater(len(colors), 8)

    def test_optional_text_overlay_writes_png(self):
        output = self.root / "overlay.png"
        apply_text_overlay(self.source, output, "Термокружка", ["Сталь 304", "500 мл"])
        self.assertTrue(output.is_file())
        with Image.open(output) as result:
            self.assertEqual(result.format, "PNG")
            self.assertEqual(result.size, (900, 1200))

    def test_exif_orientation_is_applied(self):
        source = self.root / "portrait-exif.jpg"
        image = Image.new("RGB", (120, 80), (120, 80, 40))
        exif = Image.Exif()
        exif[274] = 6
        image.save(source, format="JPEG", exif=exif)
        image_format, dimensions = validate_image_file(source)
        self.assertEqual(image_format, "JPEG")
        self.assertEqual(dimensions, (80, 120))
        output = self.root / "oriented-demo.jpg"
        make_watermarked_demo(source, output, 700)
        with Image.open(output) as result:
            self.assertEqual(result.size, (80, 120))

    def test_transparent_overlay_preserves_transparent_canvas(self):
        source = self.root / "transparent.png"
        image = Image.new("RGBA", (900, 1200), (0, 0, 0, 0))
        Image.new("RGBA", (250, 250), (200, 40, 40, 255)).save(source)
        # Build an image with a transparent canvas and a visible product area.
        image = Image.new("RGBA", (900, 1200), (0, 0, 0, 0))
        for x in range(100, 350):
            for y in range(500, 750):
                image.putpixel((x, y), (200, 40, 40, 255))
        image.save(source)
        output = self.root / "transparent-overlay.png"
        apply_text_overlay(source, output, "Товар", ["Характеристика"])
        with Image.open(output) as result:
            self.assertEqual(result.mode, "RGBA")
            self.assertEqual(result.getpixel((850, 1100))[3], 0)

    def test_validator_rejects_corrupt_file_and_size_limit(self):
        corrupt = self.root / "broken.jpg"
        corrupt.write_bytes(b"not-an-image")
        with self.assertRaises(ImagePipelineError):
            validate_image_file(corrupt)
        with self.assertRaises(ImagePipelineError):
            validate_image_file(self.source, max_bytes=10)


if __name__ == "__main__":
    unittest.main()
