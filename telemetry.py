"""Privacy-safe server-side product analytics for the Telegram bot."""
from __future__ import annotations

import os
from typing import Any

from posthog import Posthog

_api_key = os.getenv("POSTHOG_API_KEY", "").strip()
_host = os.getenv("POSTHOG_HOST", "").strip() or "https://us.i.posthog.com"

# One process-wide client. No client is created when analytics is not configured.
_client = Posthog(_api_key, host=_host, disable_geoip=True) if _api_key else None


def track_event(event: str, distinct_id: str, properties: dict[str, Any] | None = None) -> bool:
    """Queue an event without allowing analytics failures to affect bot operations.

    Only callers' explicitly supplied allowlisted, non-content properties are sent.
    Never pass Telegram IDs, names, usernames, prompts, briefs, file IDs or image data.
    """
    if _client is None:
        return False
    try:
        _client.capture(
            event,
            distinct_id=str(distinct_id),
            properties=dict(properties or {}),
            disable_geoip=True,
        )
        return True
    except Exception:
        return False


def shutdown_telemetry() -> None:
    """Flush queued analytics during graceful process shutdown."""
    if _client is not None:
        try:
            _client.shutdown()
        except Exception:
            pass
