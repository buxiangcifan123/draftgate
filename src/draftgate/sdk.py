"""Minimal client SDK built on the standard library only.

Usage::

    from draftgate import Client

    gate = Client("http://127.0.0.1:8010", token="...")
    result = gate.review(title="Weekly digest", content="...", timeout=600)
    # blocks until a human decides; returns the item dict:
    # {"id": 1, "status": "approved"|"rejected"|"edited",
    #  "content": "...", "final_content": "...", ...}
"""

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


class DraftGateError(Exception):
    """Error talking to a DraftGate server."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class DecisionTimeout(Exception):
    """No human decision within the given timeout."""


class Client:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8010",
        token: Optional[str] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token or os.environ.get("DRAFTGATE_TOKEN")

    def _request(
        self, method: str, path: str, body: Optional[dict] = None, timeout: float = 15.0
    ) -> dict:
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            self.base_url + path, data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("detail", "")
            except Exception:
                detail = ""
            message = f"HTTP {exc.code}"
            if detail:
                message += f": {detail}"
            raise DraftGateError(message, status_code=exc.code) from None
        except urllib.error.URLError as exc:
            raise DraftGateError(
                f"cannot reach DraftGate at {self.base_url}: {exc.reason}"
            ) from None

    # -- public API ---------------------------------------------------------

    def submit(
        self,
        title: str,
        content: str,
        content_type: str = "text",
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        callback_url: Optional[str] = None,
        callback_secret: Optional[str] = None,
    ) -> dict:
        """Submit a draft. Returns the created item dict (status=pending)."""
        return self._request(
            "POST",
            "/api/items",
            body={
                "title": title,
                "content": content,
                "content_type": content_type,
                "tags": tags or [],
                "metadata": metadata or {},
                "callback_url": callback_url,
                "callback_secret": callback_secret,
            },
        )

    def get(self, item_id: int) -> dict:
        """Fetch one item including its current status."""
        return self._request("GET", f"/api/items/{item_id}")

    def wait(self, item_id: int, timeout: float = 600, poll: float = 2.0) -> dict:
        """Block until the item leaves the pending state."""
        deadline = time.monotonic() + timeout
        while True:
            item = self.get(item_id)
            if item["status"] != "pending":
                return item
            if time.monotonic() >= deadline:
                raise DecisionTimeout(
                    f"item {item_id} still pending after {timeout}s"
                )
            time.sleep(poll)

    def review(
        self,
        title: str,
        content: str,
        content_type: str = "text",
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        callback_url: Optional[str] = None,
        callback_secret: Optional[str] = None,
        timeout: float = 600,
        poll: float = 2.0,
    ) -> dict:
        """Submit a draft and wait for the human decision.

        With ``timeout=0`` this is non-blocking and returns the pending item.
        """
        item = self.submit(
            title=title,
            content=content,
            content_type=content_type,
            tags=tags,
            metadata=metadata,
            callback_url=callback_url,
            callback_secret=callback_secret,
        )
        if not timeout:
            return item
        return self.wait(item["id"], timeout=timeout, poll=poll)

    def stats(self) -> dict:
        return self._request("GET", "/api/stats")
