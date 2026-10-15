"""Standalone webhook receiver that verifies DraftGate signatures.

Run it, point a callback_url at it, and watch decisions arrive::

    python examples/mock_receiver.py --port 8099 --secret my-secret

Also imported by the e2e test to assert real deliveries.
"""

import argparse
import hashlib
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List


def make_handler(secret_attr: str = "secret"):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            signature = self.headers.get("X-Draftgate-Signature", "")
            expected = "sha256=" + hmac.new(
                getattr(self.server, secret_attr).encode(), body, hashlib.sha256
            ).hexdigest()
            valid = hmac.compare_digest(signature, expected)
            try:
                payload = json.loads(body)
            except ValueError:
                payload = None
            self.server.received.append(
                {"valid": valid, "signature": signature, "payload": payload}
            )
            status = 200 if valid else 401
            self.send_response(status)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            if valid:
                item = payload or {}
                print(
                    f"[receiver] item {item.get('item_id')} -> "
                    f"{item.get('action')} (signature OK)"
                    + (f" reason={item['reason']!r}" if item.get("reason") else "")
                )
            else:
                print("[receiver] INVALID SIGNATURE - rejected")

        def log_message(self, *args):
            pass  # keep output clean

    return Handler


def start(secret: str, host: str = "127.0.0.1", port: int = 0):
    """Start a receiver in the background. Returns the HTTPServer."""
    server = ThreadingHTTPServer((host, port), make_handler())
    server.secret = secret
    server.received = []  # type: List[dict]
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def url(server) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}/callback"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument(
        "--secret",
        default=os.environ.get("DRAFTGATE_SECRET", "dev-secret"),
        help="signing secret (default: $DRAFTGATE_SECRET or 'dev-secret')",
    )
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), make_handler())
    server.secret = args.secret
    server.received = []
    print(f"receiver listening on {url(server)} (secret from --secret/env)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
