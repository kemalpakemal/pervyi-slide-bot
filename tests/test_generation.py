import base64
import io
import tempfile
import unittest
from pathlib import Path

import httpx
from PIL import Image

from generation import (
    ImageGenerationError,
    build_product_prompt,
    generate_two_images,
)


def make_png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 96), (120, 140, 160)).save(buffer, format="PNG")
    return buffer.getvalue()


PNG_BYTES = make_png_bytes()


class ImageGenerationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.product = self.root / "product.jpg"
        self.reference = self.root / "reference.jpg"
        Image.new("RGB", (80, 100), (20, 30, 40)).save(self.product, format="JPEG")
        Image.new("RGB", (80, 100), (50, 60, 70)).save(self.reference, format="JPEG")
        self.outputs = [self.root / "out1.png", self.root / "out2.png"]

    def tearDown(self):
        self.temp.cleanup()

    async def call(self, handler, **kwargs):
        transport = httpx.MockTransport(handler)
        return await generate_two_images(
            api_key="test-secret",
            base_url="https://api.example.test/v1",
            model="gpt-image-2",
            prompt="preserve the product",
            image_paths=[self.product, self.reference],
            output_paths=self.outputs,
            idempotency_key="order:abc:slide:1",
            transport=transport,
            sleep=kwargs.pop("sleep", self.no_sleep),
            jitter=lambda _a, _b: 0.0,
            **kwargs,
        )

    async def no_sleep(self, _seconds):
        return None

    async def test_success_uploads_two_images_and_stable_key(self):
        seen = []

        def handler(request):
            seen.append(request)
            b64 = base64.b64encode(PNG_BYTES).decode()
            return httpx.Response(200, json={"data": [{"b64_json": b64}, {"b64_json": b64}]})

        result = await self.call(handler)
        self.assertEqual(len(result), 2)
        for path in self.outputs:
            with Image.open(path) as generated:
                self.assertEqual(generated.format, "PNG")
                self.assertEqual(generated.size, (64, 96))
        self.assertEqual(seen[0].headers["Idempotency-Key"], "order:abc:slide:1")
        self.assertEqual(str(seen[0].url), "https://api.example.test/v1/images/edits")
        body = seen[0].read().decode("latin1")
        self.assertIn("image[]", body)
        self.assertIn("product.jpg", body)
        self.assertIn("reference.jpg", body)
        self.assertIn("n", body)

    async def test_retries_502_then_succeeds(self):
        attempts = []
        delays = []

        def handler(request):
            attempts.append(request.headers["Idempotency-Key"])
            if len(attempts) == 1:
                return httpx.Response(502, headers={"Retry-After": "3"})
            b64 = base64.b64encode(PNG_BYTES).decode()
            return httpx.Response(200, json={"data": [{"b64_json": b64}, {"b64_json": b64}]})

        async def record_sleep(value):
            delays.append(value)

        await self.call(handler, sleep=record_sleep)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(delays, [3.0])

    async def test_401_is_not_retried_and_secret_not_leaked(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(401, json={"error": {"message": "secret test-secret"}})

        with self.assertRaises(ImageGenerationError) as ctx:
            await self.call(handler)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("test-secret", str(ctx.exception))

    async def test_504_stops_after_three_attempts(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(504)

        with self.assertRaises(ImageGenerationError):
            await self.call(handler)
        self.assertEqual(len(calls), 3)

    async def test_connection_error_retries_three_times(self):
        calls = []

        def handler(request):
            calls.append(1)
            raise httpx.ConnectError("private network detail")

        with self.assertRaises(ImageGenerationError) as ctx:
            await self.call(handler)
        self.assertEqual(len(calls), 3)
        self.assertTrue(ctx.exception.uncertain)
        self.assertNotIn("private network detail", str(ctx.exception))

    async def test_missing_api_key_fails_without_network(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(200, json={"data": []})

        with self.assertRaises(ImageGenerationError):
            await generate_two_images(
                api_key="",
                base_url="https://api.example.test/v1",
                model="gpt-image-2",
                prompt="prompt",
                image_paths=[self.product],
                output_paths=self.outputs,
                idempotency_key="order:no-key",
                transport=httpx.MockTransport(handler),
                sleep=self.no_sleep,
            )
        self.assertEqual(calls, [])

    async def test_503_stops_after_three_attempts(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(503)

        with self.assertRaises(ImageGenerationError):
            await self.call(handler)
        self.assertEqual(len(calls), 3)


    async def test_quota_429_is_not_retried(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(
                429,
                json={"error": {"code": "insufficient_quota", "message": "private account details"}},
            )

        with self.assertRaises(ImageGenerationError) as ctx:
            await self.call(handler)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("private account details", str(ctx.exception))

    async def test_non_retryable_400_is_not_retried(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(400, json={"error": {"message": "private request detail"}})

        with self.assertRaises(ImageGenerationError):
            await self.call(handler)
        self.assertEqual(len(calls), 1)

    async def test_timeout_retries_three_times(self):
        calls = []

        def handler(request):
            calls.append(1)
            raise httpx.ReadTimeout("internal test data")

        with self.assertRaises(ImageGenerationError) as ctx:
            await self.call(handler)
        self.assertEqual(len(calls), 3)
        self.assertTrue(ctx.exception.uncertain)
        self.assertNotIn("internal test data", str(ctx.exception))


    async def test_402_balance_error_is_not_retried(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(402, json={"error": {"message": "private billing data"}})

        with self.assertRaises(ImageGenerationError) as ctx:
            await self.call(handler)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("private billing data", str(ctx.exception))

    async def test_408_client_timeout_status_is_not_retried(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(408)

        with self.assertRaises(ImageGenerationError):
            await self.call(handler)
        self.assertEqual(len(calls), 1)

    async def test_malformed_200_is_classified_as_successful_request_but_uncertain_result(self):
        def handler(request):
            return httpx.Response(200, json={"data": []})

        with self.assertRaises(ImageGenerationError) as ctx:
            await self.call(handler)
        self.assertTrue(ctx.exception.request_succeeded)
        self.assertTrue(ctx.exception.uncertain)

    async def test_corrupt_image_payload_is_rejected_after_successful_http_request(self):
        def handler(request):
            bad = base64.b64encode(b"not-an-image").decode()
            return httpx.Response(200, json={"data": [{"b64_json": bad}, {"b64_json": bad}]})

        with self.assertRaises(ImageGenerationError) as ctx:
            await self.call(handler)
        self.assertTrue(ctx.exception.request_succeeded)
        self.assertTrue(ctx.exception.uncertain)
        self.assertFalse(self.outputs[0].exists())
        self.assertFalse(self.outputs[1].exists())

    async def test_invalid_input_is_rejected_before_network_request(self):
        self.product.write_bytes(b"not-an-image")
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(200, json={"data": []})

        with self.assertRaises(ImageGenerationError):
            await self.call(handler)
        self.assertEqual(calls, [])

    async def test_prompt_preserves_product_identity(self):
        prompt = build_product_prompt({
            "product": "Термокружка",
            "platform": "WB",
            "advantages": "Сталь 304",
            "required_text": "500 мл",
            "style": "минимализм",
            "colors": "чёрный",
        }, 1, 2)
        self.assertIn("Сохрани без изменений геометрию", prompt)
        self.assertIn("этикетку", prompt)
        self.assertIn("500 мл", prompt)


if __name__ == "__main__":
    unittest.main()
