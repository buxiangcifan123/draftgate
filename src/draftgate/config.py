"""Configuration: environment variables take priority over the config file.

Config lives in ``~/.draftgate/config.json`` (override the directory with
``DRAFTGATE_HOME``). A random API token and webhook secret are generated on
first run. Everything can also be provided via environment variables, which
keeps containerized / CI usage zero-touch:

- ``DRAFTGATE_TOKEN``   API token (auth for /api/*)
- ``DRAFTGATE_SECRET``  default webhook signing secret
- ``DRAFTGATE_DB``      SQLite database file path
- ``DRAFTGATE_HOME``    config directory (default ~/.draftgate)
"""

import json
import os
import secrets
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8010


def config_dir() -> Path:
    override = os.environ.get("DRAFTGATE_HOME")
    if override:
        return Path(override)
    return Path.home() / ".draftgate"


def config_path() -> Path:
    return config_dir() / "config.json"


def db_path() -> Path:
    env = os.environ.get("DRAFTGATE_DB")
    if env:
        return Path(env)
    return config_dir() / "draftgate.db"


def _load_file_config() -> dict:
    path = config_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def ensure_config() -> dict:
    """Load config, generating token/secret on first run. Returns full config."""
    cfg = _load_file_config()
    changed = False
    if not cfg.get("token"):
        cfg["token"] = secrets.token_urlsafe(32)
        changed = True
    if not cfg.get("secret"):
        cfg["secret"] = secrets.token_urlsafe(32)
        changed = True
    if changed:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg


def get_token() -> str:
    env = os.environ.get("DRAFTGATE_TOKEN")
    if env:
        return env
    return ensure_config()["token"]


def has_explicit_token() -> bool:
    """True when a token was set deliberately (env var or saved config).

    ``get_token()`` always returns something because it generates a token on
    first run, so it cannot answer "did the user choose this?". Exposing the
    server beyond localhost should require a token the user has actually seen.
    """
    if os.environ.get("DRAFTGATE_TOKEN"):
        return True
    return bool(_load_file_config().get("token"))


def get_secret() -> str:
    env = os.environ.get("DRAFTGATE_SECRET")
    if env:
        return env
    return ensure_config()["secret"]
