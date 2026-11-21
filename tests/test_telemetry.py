import unittest
from unittest.mock import Mock, patch

import telemetry


class TelemetryTests(unittest.TestCase):
    def test_event_uses_only_explicit_properties_and_pseudonymous_id(self):
        client = Mock()
        with patch.object(telemetry, "_client", client):
            result = telemetry.track_event(
                "order_submitted",
                "internal-client-42",
                {"platform": "WB", "slides": 3},
            )
        self.assertTrue(result)
        client.capture.assert_called_once_with(
            "order_submitted",
            distinct_id="internal-client-42",
            properties={"platform": "WB", "slides": 3},
            disable_geoip=True,
        )

    def test_capture_failure_does_not_escape(self):
        client = Mock()
        client.capture.side_effect = RuntimeError("analytics offline")
        with patch.object(telemetry, "_client", client):
            self.assertFalse(telemetry.track_event("generation_started", "42", {}))

    def test_disabled_analytics_is_noop(self):
        with patch.object(telemetry, "_client", None):
            self.assertFalse(telemetry.track_event("order_submitted", "42", {}))

    def test_shutdown_is_safe_when_disabled(self):
        with patch.object(telemetry, "_client", None):
            telemetry.shutdown_telemetry()


if __name__ == "__main__":
    unittest.main()
