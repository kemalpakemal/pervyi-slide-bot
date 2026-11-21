#!/usr/bin/env python3
"""Phone-side polling worker for the existing First Slide Telegram bot."""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request


BASE_URL = os.environ.get("HERMES_BRIDGE_URL", "").rstrip("/")
TOKEN = os.environ.get("HERMES_BRIDGE_TOKEN", "")
HERMES_BIN = os.environ.get("HERMES_BIN", "hermes")
POLL_SECONDS = max(2, int(os.environ.get("HERMES_BRIDGE_POLL_SECONDS", "5")))
TIMEOUT_SECONDS = max(60, int(os.environ.get("HERMES_BRIDGE_TASK_TIMEOUT", "1800")))


def api(method: str, path: str, payload: dict | None = None) -> dict:
    if not BASE_URL or not TOKEN:
        raise RuntimeError("Set HERMES_BRIDGE_URL and HERMES_BRIDGE_TOKEN first.")
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + TOKEN,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Hermes-Phone-Bridge/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=35) as response:
        return json.loads(response.read().decode("utf-8"))


def run_job(job: dict) -> None:
    job_id = str(job["id"])
    prompt = str(job["prompt"])
    print(f"[Hermes Bridge] Running {job_id[:8]}...", flush=True)
    try:
        completed = subprocess.run(
            [HERMES_BIN, "-z", prompt],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
        output = (completed.stdout or "").strip()
        if completed.returncode != 0:
            error = (completed.stderr or output or f"hermes exited with code {completed.returncode}").strip()
            api("POST", "/_bridge/hermes/result", {
                "job_id": job_id, "status": "failed", "error": error[-4000:],
            })
            print(f"[Hermes Bridge] Failed {job_id[:8]} (exit {completed.returncode})", flush=True)
            return
        api("POST", "/_bridge/hermes/result", {
            "job_id": job_id, "status": "done",
            "result": (output or "Hermes завершил задачу без текстового ответа.")[-24000:],
        })
        print(f"[Hermes Bridge] Completed {job_id[:8]}", flush=True)
    except subprocess.TimeoutExpired:
        api("POST", "/_bridge/hermes/result", {
            "job_id": job_id, "status": "failed",
            "error": f"Задача превысила лимит {TIMEOUT_SECONDS} секунд.",
        })


def main() -> int:
    if not BASE_URL or not TOKEN:
        print("Set HERMES_BRIDGE_URL and HERMES_BRIDGE_TOKEN.", file=sys.stderr)
        return 2
    try:
        print(api("GET", "/_bridge/hermes/health"), flush=True)
    except Exception as exc:
        print(f"Bridge connection failed: {exc}", file=sys.stderr)
        return 1
    print("[Hermes Bridge] Connected. Waiting for Telegram /agent jobs.", flush=True)
    while True:
        try:
            response = api("GET", "/_bridge/hermes/next")
            job = response.get("job")
            if job:
                run_job(job)
            else:
                time.sleep(POLL_SECONDS)
        except urllib.error.HTTPError as exc:
            print(f"[Hermes Bridge] HTTP {exc.code}: {exc.reason}", file=sys.stderr, flush=True)
            time.sleep(max(POLL_SECONDS, 10))
        except Exception as exc:
            print(f"[Hermes Bridge] Network error: {exc}", file=sys.stderr, flush=True)
            time.sleep(max(POLL_SECONDS, 10))


if __name__ == "__main__":
    raise SystemExit(main())
