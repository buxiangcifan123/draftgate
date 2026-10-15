"""Webhook delivery: HMAC-signed, retried with backoff.

Every decision on an item that was submitted with a ``callback_url`` produces
one delivery row inside the same transaction as the decision itself, so a
crash can never lose a callback. The first attempt happens synchronously right
after the decision; failures are retried by a background worker with
10s / 1m / 5m backoff, then marked ``failed``. A manual retry endpoint resets
failed deliveries.

Signature header::

    X-Draftgate-Signature: sha256=<hex hmac of raw body>
"""

import hashlib
import hmac
import json
import threading
import traceback
import urllib.error
import urllib.request
from typing import Optional, Tuple

from . import config, db
from ._version import __version__

RETRY_BACKOFF_SECONDS = [10, 60, 300]
MAX_ATTEMPTS = 1 + len(RETRY_BACKOFF_SECONDS)  # sync attempt + 3 retries
POLL_INTERVAL = 5.0
HTTP_TIMEOUT = 5.0
# The first attempt runs inside the decision request, so a dead callback would
# otherwise stall the reviewer's click for the full HTTP_TIMEOUT. Keep it short:
# anything slower than this falls through to the background retries.
SYNC_HTTP_TIMEOUT = 2.0

_ACTION_BY_STATUS = {"approved": "approve", "rejected": "reject", "edited": "edit"}


def sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return "sha256=" + digest


def build_payload(item: dict) -> bytes:
    final = item["final_content"] if item["final_content"] is not None else item["content"]
    payload = {
        "item_id": item["id"],
        "action": _ACTION_BY_STATUS[item["status"]],
        "status": item["status"],
        "title": item["title"],
        "content": final,
        "reason": item["reason"],
        "tags": item["tags"],
        "metadata": item["metadata"],
        "decided_at": item["decided_at"],
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def deliver_once(
    item: dict, secret: str, timeout: float = HTTP_TIMEOUT
) -> Tuple[bool, str]:
    """One delivery attempt. Returns (ok, error_message)."""
    body = build_payload(item)
    request = urllib.request.Request(
        item["callback_url"],
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Draftgate-Event": "decision",
            "X-Draftgate-Signature": sign(secret, body),
            "User-Agent": f"DraftGate/{__version__}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if 200 <= response.status < 300:
                return True, ""
            return False, f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # network errors, timeouts, DNS, ...
        return False, f"{type(exc).__name__}: {exc}"


def _secret_for(item: dict) -> str:
    return item.get("callback_secret") or config.get_secret()


def deliver_after_decision(item: dict, delivery_id: int) -> None:
    """Fast path: synchronous first attempt right after a decision.

    Uses ``SYNC_HTTP_TIMEOUT`` so a slow or dead callback cannot park the
    reviewer's click; the background worker retries the rest.
    """
    ok, error = deliver_once(item, _secret_for(item), timeout=SYNC_HTTP_TIMEOUT)
    if ok:
        db.delivery_delivered(delivery_id, attempts=1)
    else:
        db.schedule_retry(
            delivery_id,
            attempts=1,
            error=error,
            next_retry_at=db.time_after(RETRY_BACKOFF_SECONDS[0]),
        )


class RetryWorker(threading.Thread):
    """Background worker that retries pending deliveries."""

    def __init__(self) -> None:
        super().__init__(daemon=True, name="draftgate-retry-worker")
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:  # never let the worker die
                traceback.print_exc()
            self._stop_event.wait(POLL_INTERVAL)

    def _tick(self) -> None:
        for delivery in db.due_deliveries(db.utcnow()):
            self._attempt(delivery)

    def _attempt(self, delivery: dict) -> None:
        item = db.get_item(delivery["item_id"])
        if item is None or not item.get("callback_url"):
            db.mark_delivery_failed(
                delivery["id"], delivery["attempts"], "item or callback_url missing"
            )
            return
        ok, error = deliver_once(item, _secret_for(item))
        attempts = delivery["attempts"] + 1
        if ok:
            db.delivery_delivered(delivery["id"], attempts)
        elif attempts >= MAX_ATTEMPTS:
            db.mark_delivery_failed(delivery["id"], attempts, error)
        else:
            backoff = RETRY_BACKOFF_SECONDS[
                min(attempts - 1, len(RETRY_BACKOFF_SECONDS) - 1)
            ]
            db.schedule_retry(
                delivery["id"], attempts, error, db.time_after(backoff)
            )


_worker: Optional[RetryWorker] = None


def start_worker() -> None:
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    _worker = RetryWorker()
    _worker.start()


def stop_worker() -> None:
    global _worker
    if _worker is not None:
        _worker.stop()
        _worker = None
