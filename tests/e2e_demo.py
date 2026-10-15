"""End-to-end verification without pytest: real server + real CLI + real webhook.

Run with the dev environment::

    python tests/e2e_demo.py

Covers: submit (blocking + non-blocking), all three decisions, CLI exit codes
(0=approved, 1=rejected, 2=edited), webhook delivery with HMAC verification,
and the `token` command.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))
sys.path.insert(0, str(ROOT / "src"))

TOKEN = "e2e-token"
SECRET = "e2e-secret"
BASE_URL = "http://127.0.0.1:8010"


def http(method: str, path: str, body: dict = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {TOKEN}",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode())


def run_cli(*args: str, timeout: float = 60.0):
    env = dict(
        os.environ,
        DRAFTGATE_TOKEN=TOKEN,
        DRAFTGATE_SECRET=SECRET,
        PYTHONIOENCODING="utf-8",
    )
    return subprocess.run(
        [sys.executable, "-m", "draftgate", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=timeout,
    )


def wait_for_server(deadline: float = 15.0) -> None:
    start = time.monotonic()
    while time.monotonic() - start < deadline:
        try:
            with urllib.request.urlopen(BASE_URL + "/health", timeout=2) as r:
                if r.status == 200:
                    return
        except urllib.error.URLError:
            time.sleep(0.3)
    raise SystemExit("server did not come up")


def decide_later(item_id: int, action: str, content: str = None, delay: float = 1.0):
    """Simulate the human clicking in the UI, after a short delay."""
    def worker():
        time.sleep(delay)
        body = {"action": action}
        if content is not None:
            body["content"] = content
        http("POST", f"/api/items/{item_id}/decision", body)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="draftgate-e2e-")
    os.environ["DRAFTGATE_HOME"] = tmp  # isolated config dir for this process
    os.environ["DRAFTGATE_DB"] = os.path.join(tmp, "e2e.db")
    os.environ["DRAFTGATE_TOKEN"] = TOKEN  # server process must accept these
    os.environ["DRAFTGATE_SECRET"] = SECRET

    import mock_receiver

    receiver = mock_receiver.start(SECRET)
    callback = mock_receiver.url(receiver)
    print(f"[e2e] receiver on {callback}")

    import uvicorn

    from draftgate.api import create_app

    server = uvicorn.Server(
        uvicorn.Config(create_app(), host="127.0.0.1", port=8010, log_level="warning")
    )
    threading.Thread(target=server.run, daemon=True).start()
    wait_for_server()
    print("[e2e] server up")

    checks = 0

    # -- token command --------------------------------------------------
    result = run_cli("token")
    assert result.stdout.strip() == TOKEN, f"token cmd: {result.stdout!r}"
    checks += 1
    print("[e2e] token command OK")

    # -- blocking submit + approve -> exit 0 ----------------------------
    stdin_file = Path(tmp) / "draft.md"
    stdin_file.write_text("# please approve\nthis draft.", encoding="utf-8")

    def submit_with_file():
        env = dict(os.environ, DRAFTGATE_TOKEN=TOKEN, DRAFTGATE_SECRET=SECRET,
                   PYTHONIOENCODING="utf-8")
        return subprocess.run(
            [sys.executable, "-m", "draftgate", "submit",
             "--title", "Approve me", "--file", str(stdin_file),
             "--type", "markdown", "--tag", "demo", "--meta", "source=e2e",
             "--callback-url", callback, "--callback-secret", SECRET,
             "--timeout", "30"],
            capture_output=True, text=True, encoding="utf-8", env=env, timeout=60,
        )

    # find the item id (submit prints JSON) - approve it after 1s while CLI waits
    def approve_newest():
        time.sleep(1.2)
        items = http("GET", "/api/items?status=pending")
        match = [i for i in items if i["title"] == "Approve me"]
        assert match, "expected a pending item titled 'Approve me'"
        http("POST", f"/api/items/{match[0]['id']}/decision", {"action": "approve"})

    threading.Thread(target=approve_newest, daemon=True).start()
    result = submit_with_file()
    assert result.returncode == 0, (
        f"approve exit: {result.returncode}\nstdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == "approved"
    checks += 1
    print("[e2e] blocking submit + approve -> exit 0 OK")

    # -- non-blocking submit + reject -> exit 1 --------------------------
    item = http("POST", "/api/items", {
        "title": "Reject me",
        "content": "bad draft",
        "callback_url": callback,
        "callback_secret": SECRET,
    })
    http("POST", f"/api/items/{item['id']}/decision",
         {"action": "reject", "reason": "off-brand tone"})
    result = run_cli("wait", str(item["id"]), "--timeout", "30")
    assert result.returncode == 1, f"reject exit: {result.returncode}"
    payload = json.loads(result.stdout)
    assert payload["reason"] == "off-brand tone", payload
    checks += 1
    print("[e2e] reject -> exit 1, reviewer reason OK")

    # -- edit -> exit 2 ---------------------------------------------------
    item = http("POST", "/api/items", {
        "title": "Edit me",
        "content": "original",
        "callback_url": callback,
        "callback_secret": SECRET,
    })
    http("POST", f"/api/items/{item['id']}/decision",
         {"action": "edit", "content": "edited by human"})
    result = run_cli("wait", str(item["id"]), "--timeout", "30")
    assert result.returncode == 2, f"edit exit: {result.returncode}"
    payload = json.loads(result.stdout)
    assert payload["final_content"] == "edited by human"
    checks += 1
    print("[e2e] edit -> exit 2, final_content OK")

    # -- webhooks all delivered with valid signatures ---------------------
    time.sleep(1.0)  # allow fast-path deliveries to land
    deliveries = receiver.received
    assert len(deliveries) == 3, f"expected 3 webhook deliveries, got {len(deliveries)}"
    assert all(d["valid"] for d in deliveries), "invalid signature detected"
    actions = sorted(d["payload"]["action"] for d in deliveries)
    assert actions == ["approve", "edit", "reject"], actions
    by_action = {d["payload"]["action"]: d["payload"] for d in deliveries}
    assert by_action["reject"]["reason"] == "off-brand tone", by_action["reject"]
    assert by_action["approve"]["reason"] is None, by_action["approve"]
    checks += 1
    print("[e2e] 3/3 webhooks delivered, HMAC signatures valid")

    print(f"\n[e2e] ALL {checks} CHECKS PASSED")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"\n[e2e] FAILED: {exc}")
        sys.exit(1)
