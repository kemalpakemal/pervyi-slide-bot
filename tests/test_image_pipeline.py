import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageChops

from image_pipeline import apply_text_overlay, make_watermarked_demo


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
            # A visible watermark must change the otherwise uniform source image.
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


if __name__ == "__main__":
    unittest.main()
