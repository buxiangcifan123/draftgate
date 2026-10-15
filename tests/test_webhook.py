"""Tests for webhook signing and delivery (synchronous fast path)."""

import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from draftgate import config, db, webhooks
from tests.conftest import TEST_SECRET


class _Receiver(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        signature = self.headers.get("X-Draftgate-Signature", "")
        expected = "sha256=" + hmac.new(
            self.server.secret.encode(), body, hashlib.sha256
        ).hexdigest()
        valid = hmac.compare_digest(signature, expected)
        self.server.received.append(
            {"valid": valid, "signature": signature, "payload": json.loads(body)}
        )
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):  # silence
        pass


@pytest.fixture
def receiver():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Receiver)
    server.secret = TEST_SECRET
    server.received = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DRAFTGATE_DB", str(tmp_path / "wh.db"))
    monkeypatch.setenv("DRAFTGATE_TOKEN", "t")
    monkeypatch.setenv("DRAFTGATE_SECRET", TEST_SECRET)
    db.init_db()
    yield


def _sign(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_sign_format_and_tamper_detection():
    body = b'{"a": 1}'
    signature = webhooks.sign("s3cret", body)
    assert signature.startswith("sha256=")
    assert len(signature) == len("sha256=") + 64
    # different secret or body -> different signature
    assert webhooks.sign("other", body) != signature
    assert webhooks.sign("s3cret", b'{"a": 2}') != signature


def test_payload_uses_final_content():
    item = db.create_item(title="T", content="original")
    item = db.decide_item(item["id"], "edit", "fixed")[0]
    payload = json.loads(webhooks.build_payload(item))
    assert payload["content"] == "fixed"
    assert payload["action"] == "edit"
    assert payload["status"] == "edited"
    assert payload["item_id"] == item["id"]
    # approve keeps original content
    item2 = db.create_item(title="T2", content="keep me")
    item2 = db.decide_item(item2["id"], "approve", None)[0]
    payload2 = json.loads(webhooks.build_payload(item2))
    assert payload2["content"] == "keep me"
    assert payload2["action"] == "approve"


def test_payload_includes_reason():
    item = db.create_item(title="T", content="C")
    item = db.decide_item(item["id"], "reject", None, reason="off-brand tone")[0]
    payload = json.loads(webhooks.build_payload(item))
    assert payload["reason"] == "off-brand tone"
    assert payload["action"] == "reject"
    # no reason given -> explicit null, so receivers can key on the field
    item2 = db.create_item(title="T2", content="C")
    item2 = db.decide_item(item2["id"], "approve", None)[0]
    assert json.loads(webhooks.build_payload(item2))["reason"] is None


def test_sync_delivery_with_signature_verification(api, auth, receiver):
    callback = f"http://127.0.0.1:{receiver.server_address[1]}/callback"
    item = api.post(
        "/api/items",
        json={
            "title": "with webhook",
            "content": "body",
            "callback_url": callback,
        },
        headers=auth,
    ).json()
    resp = api.post(
        f"/api/items/{item['id']}/decision", json={"action": "approve"}, headers=auth
    )
    # decision response already reflects delivery outcome (fast path)
    assert resp.json()["webhook"]["status"] == "delivered"
    assert len(receiver.received) == 1
    received = receiver.received[0]
    assert received["valid"] is True
    assert received["payload"]["item_id"] == item["id"]
    assert received["payload"]["action"] == "approve"
    assert received["payload"]["content"] == "body"


def test_failed_delivery_scheduled_for_retry(api, auth):
    # port 9 is never listening -> connection refused
    item = api.post(
        "/api/items",
        json={
            "title": "dead webhook",
            "content": "body",
            "callback_url": "http://127.0.0.1:9/callback",
        },
        headers=auth,
    ).json()
    resp = api.post(
        f"/api/items/{item['id']}/decision", json={"action": "approve"}, headers=auth
    )
    webhook_info = resp.json()["webhook"]
    assert webhook_info["status"] == "pending"
    assert webhook_info["attempts"] == 1
    assert webhook_info["last_error"]

    # manual retry resets and tries again (still fails, stays scheduled)
    resp = api.post(
        f"/api/items/{item['id']}/webhook/retry", headers=auth
    )
    assert resp.json()["webhook"]["attempts"] >= 1


def test_retry_endpoint_requires_callback(api, auth):
    item = api.post(
        "/api/items", json={"title": "no cb", "content": "c"}, headers=auth
    ).json()
    api.post(f"/api/items/{item['id']}/decision", json={"action": "approve"}, headers=auth)
    resp = api.post(f"/api/items/{item['id']}/webhook/retry", headers=auth)
    assert resp.status_code == 409
