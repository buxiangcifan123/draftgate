"""Command line interface.

Commands::

    draftgate serve [--host H] [--port P]      run the review station
    draftgate submit --title T --file F ...    submit a draft (optionally wait)
    draftgate wait <id> [--timeout S]          wait for a decision
    draftgate token                            print the current API token

``submit --timeout S`` (S > 0) and ``wait`` exit with:
0 = approved, 1 = rejected, 2 = edited, 3 = still pending (timeout).
The final item is printed to stdout as JSON so n8n / shell scripts can
branch on it.
"""

import argparse
import json
import sys
from typing import List, Optional

from . import config
from ._version import __version__
from .sdk import Client, DecisionTimeout, DraftGateError

EXIT_APPROVED = 0
EXIT_REJECTED = 1
EXIT_EDITED = 2
EXIT_PENDING = 3

_EXIT_BY_STATUS = {
    "approved": EXIT_APPROVED,
    "rejected": EXIT_REJECTED,
    "edited": EXIT_EDITED,
}


def _force_utf8_stdout() -> None:
    """Windows consoles default to a legacy code page; force UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass


def _print_json(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _parse_meta(pairs: Optional[List[str]]) -> dict:
    meta: dict = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"invalid --meta (expected key=value): {pair}")
        key, value = pair.split("=", 1)
        meta[key] = value
    return meta


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------

def cmd_serve(args: argparse.Namespace) -> int:
    if args.host not in ("127.0.0.1", "localhost") and not config.has_explicit_token():
        print(
            f"refusing to bind {args.host} without an explicit API token.\n"
            "The queue and API would be reachable from the network, guarded only\n"
            "by a token generated on your behalf. Set one you know first:\n"
            f"    DRAFTGATE_TOKEN=<your-token> draftgate serve --host {args.host}\n"
            "(`draftgate token` prints the one already configured locally.)",
            file=sys.stderr,
        )
        return 2

    import uvicorn

    from .api import create_app

    app = create_app()
    token = config.get_token()
    print(f"DraftGate {__version__} listening on http://{args.host}:{args.port}")
    print(f"API token: {token}")
    print("Open the review UI in your browser to start reviewing.")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    if args.file == "-":
        content = sys.stdin.read()
    else:
        with open(args.file, "r", encoding="utf-8") as handle:
            content = handle.read()
    if not content.strip():
        print("content is empty", file=sys.stderr)
        return 2

    client = Client(args.url)
    try:
        item = client.submit(
            title=args.title,
            content=content,
            content_type=args.type,
            tags=args.tag or [],
            metadata=_parse_meta(args.meta),
            callback_url=args.callback_url,
            callback_secret=args.callback_secret,
        )
    except DraftGateError as exc:
        print(f"submit failed: {exc}", file=sys.stderr)
        return 2

    if not args.timeout:
        _print_json(item)
        return EXIT_PENDING

    try:
        final = client.wait(item["id"], timeout=args.timeout, poll=args.poll)
    except DecisionTimeout:
        _print_json(item)
        print(f"still pending after {args.timeout}s", file=sys.stderr)
        return EXIT_PENDING
    _print_json(final)
    return _EXIT_BY_STATUS[final["status"]]


def cmd_wait(args: argparse.Namespace) -> int:
    client = Client(args.url)
    try:
        final = client.wait(args.item_id, timeout=args.timeout, poll=args.poll)
    except DecisionTimeout:
        print(f"item {args.item_id} still pending after {args.timeout}s", file=sys.stderr)
        return EXIT_PENDING
    except DraftGateError as exc:
        print(f"wait failed: {exc}", file=sys.stderr)
        return 2
    _print_json(final)
    return _EXIT_BY_STATUS[final["status"]]


def cmd_token(_args: argparse.Namespace) -> int:
    print(config.get_token())
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="draftgate",
        description="The human checkpoint for AI content pipelines.",
    )
    parser.add_argument("--version", action="version", version=f"draftgate {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="start the review station server")
    serve.add_argument("--host", default=config.DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    serve.set_defaults(func=cmd_serve)

    submit = sub.add_parser("submit", help="submit a draft for review")
    submit.add_argument("--title", required=True)
    submit.add_argument(
        "--file", required=True,
        help="path to the content file, or '-' to read stdin",
    )
    submit.add_argument("--type", choices=["text", "markdown", "json"], default="text")
    submit.add_argument("--tag", action="append", help="repeatable tag")
    submit.add_argument(
        "--meta", action="append", metavar="KEY=VALUE",
        help="repeatable metadata entry",
    )
    submit.add_argument("--callback-url", default=None)
    submit.add_argument("--callback-secret", default=None)
    submit.add_argument("--url", default="http://127.0.0.1:8010")
    submit.add_argument(
        "--timeout", type=int, default=0,
        help="seconds to wait for a decision (0 = submit only)",
    )
    submit.add_argument("--poll", type=float, default=2.0)
    submit.set_defaults(func=cmd_submit)

    wait = sub.add_parser("wait", help="wait for a human decision")
    wait.add_argument("item_id", type=int)
    wait.add_argument("--timeout", type=int, default=600)
    wait.add_argument("--url", default="http://127.0.0.1:8010")
    wait.add_argument("--poll", type=float, default=2.0)
    wait.set_defaults(func=cmd_wait)

    token = sub.add_parser("token", help="print the current API token")
    token.set_defaults(func=cmd_token)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    _force_utf8_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
