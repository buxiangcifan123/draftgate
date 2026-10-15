"""SQLite storage: zero-config, WAL mode, one connection per operation.

Times are stored as UTC strings in ``YYYY-MM-DD HH:MM:SS`` format so that
lexicographic comparison equals chronological comparison.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  content TEXT NOT NULL,
  final_content TEXT,
  content_type TEXT NOT NULL DEFAULT 'text',
  tags TEXT NOT NULL DEFAULT '[]',
  metadata TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending',
  callback_url TEXT,
  callback_secret TEXT,
  created_at TEXT NOT NULL,
  decided_at TEXT,
  reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status, created_at);

CREATE TABLE IF NOT EXISTS webhook_deliveries(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES items(id),
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  next_retry_at TEXT,
  delivered_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deliveries ON webhook_deliveries(status, next_retry_at);
"""

# Initial grace period before the background worker may pick up a fresh
# delivery. The synchronous first attempt happens immediately after a decision;
# this window avoids double delivery races while still recovering deliveries
# if the process crashes right after deciding.
INITIAL_DELIVERY_GRACE_SECONDS = 15


class NotFoundError(Exception):
    """Requested item does not exist."""


class ConflictError(Exception):
    """Item is not in a state that allows this operation."""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def time_after(seconds: float) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=seconds)
    ).strftime("%Y-%m-%d %H:%M:%S")


def connect() -> sqlite3.Connection:
    path = config.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None  # explicit transaction control
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the first release to existing databases."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
    if "reason" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN reason TEXT")


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(_SCHEMA)
        _migrate(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# items
# ---------------------------------------------------------------------------

_ITEM_COLUMNS = (
    "id, title, content, final_content, content_type, tags, metadata, "
    "status, callback_url, callback_secret, created_at, decided_at, reason"
)


def _row_to_item(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["tags"] = json.loads(item["tags"])
    item["metadata"] = json.loads(item["metadata"])
    return item


def create_item(
    title: str,
    content: str,
    content_type: str = "text",
    tags: Optional[List[str]] = None,
    metadata: Optional[dict] = None,
    callback_url: Optional[str] = None,
    callback_secret: Optional[str] = None,
) -> dict:
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO items(title, content, content_type, tags, metadata,"
            " status, callback_url, callback_secret, created_at)"
            " VALUES(?,?,?,?,?, 'pending', ?, ?, ?)",
            (
                title,
                content,
                content_type,
                json.dumps(tags or []),
                json.dumps(metadata or {}),
                callback_url,
                callback_secret,
                utcnow(),
            ),
        )
        item_id = cur.lastrowid
    finally:
        conn.close()
    return get_item(item_id)  # type: ignore[return-value]


def get_item(item_id: int) -> Optional[dict]:
    conn = connect()
    try:
        row = conn.execute(
            f"SELECT {_ITEM_COLUMNS} FROM items WHERE id = ?", (item_id,)
        ).fetchone()
    finally:
        conn.close()
    return _row_to_item(row) if row else None


def list_items(
    status: Optional[str] = None, limit: int = 50, offset: int = 0
) -> List[dict]:
    conn = connect()
    try:
        if status:
            rows = conn.execute(
                f"SELECT {_ITEM_COLUMNS} FROM items WHERE status = ?"
                " ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (status, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_ITEM_COLUMNS} FROM items"
                " ORDER BY CASE WHEN status = 'pending' THEN 0 ELSE 1 END,"
                " created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
    finally:
        conn.close()
    return [_row_to_item(r) for r in rows]


def stats() -> dict:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM items GROUP BY status"
        ).fetchall()
    finally:
        conn.close()
    result = {"pending": 0, "approved": 0, "rejected": 0, "edited": 0, "total": 0}
    for row in rows:
        result[row["status"]] = row["n"]
        result["total"] += row["n"]
    return result


def decide_item(
    item_id: int,
    action: str,
    final_content: Optional[str],
    reason: Optional[str] = None,
) -> Tuple[dict, Optional[int]]:
    """Apply a decision in one transaction.

    ``reason`` is the reviewer's free-text note (typically why a draft was
    rejected); it is optional for every action and travels back to the
    pipeline in the webhook payload.

    Returns ``(item, delivery_id)`` where ``delivery_id`` is ``None`` when the
    item has no callback_url. Raises :class:`NotFoundError` /
    :class:`ConflictError` as appropriate.
    """
    status_map = {"approve": "approved", "reject": "rejected", "edit": "edited"}
    status = status_map[action]
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, status, callback_url FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            raise NotFoundError(f"item {item_id} not found")
        if row["status"] != "pending":
            conn.execute("ROLLBACK")
            raise ConflictError(
                f"item {item_id} already decided ({row['status']})"
            )
        conn.execute(
            "UPDATE items SET status = ?, final_content = ?, reason = ?,"
            " decided_at = ? WHERE id = ?",
            (status, final_content, reason, utcnow(), item_id),
        )
        delivery_id = None
        if row["callback_url"]:
            cur = conn.execute(
                "INSERT INTO webhook_deliveries(item_id, status, next_retry_at,"
                " created_at) VALUES(?, 'pending', ?, ?)",
                (item_id, time_after(INITIAL_DELIVERY_GRACE_SECONDS), utcnow()),
            )
            delivery_id = cur.lastrowid
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass  # transaction already closed
        raise
    finally:
        conn.close()
    return get_item(item_id), delivery_id  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# webhook deliveries
# ---------------------------------------------------------------------------

def get_delivery_by_item(item_id: int) -> Optional[dict]:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id, item_id, status, attempts, last_error, next_retry_at,"
            " delivered_at, created_at FROM webhook_deliveries"
            " WHERE item_id = ? ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def due_deliveries(now: str) -> List[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT d.id, d.item_id, d.attempts FROM webhook_deliveries d"
            " WHERE d.status = 'pending'"
            " AND (d.next_retry_at IS NULL OR d.next_retry_at <= ?)"
            " ORDER BY d.id LIMIT 50",
            (now,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def delivery_delivered(delivery_id: int, attempts: int) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE webhook_deliveries SET status = 'delivered', attempts = ?,"
            " delivered_at = ?, last_error = NULL, next_retry_at = NULL"
            " WHERE id = ?",
            (attempts, utcnow(), delivery_id),
        )
    finally:
        conn.close()


def schedule_retry(
    delivery_id: int, attempts: int, error: str, next_retry_at: str
) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE webhook_deliveries SET attempts = ?, last_error = ?,"
            " next_retry_at = ? WHERE id = ?",
            (attempts, error, next_retry_at, delivery_id),
        )
    finally:
        conn.close()


def mark_delivery_failed(delivery_id: int, attempts: int, error: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE webhook_deliveries SET status = 'failed', attempts = ?,"
            " last_error = ?, next_retry_at = NULL WHERE id = ?",
            (attempts, error, delivery_id),
        )
    finally:
        conn.close()


def reset_delivery(delivery_id: int) -> None:
    """Manual retry: make the delivery due again (with grace window so the
    synchronous attempt right after this call is not double-fired by the
    background worker)."""
    conn = connect()
    try:
        conn.execute(
            "UPDATE webhook_deliveries SET status = 'pending', attempts = 0,"
            " next_retry_at = ? WHERE id = ?",
            (time_after(INITIAL_DELIVERY_GRACE_SECONDS), delivery_id),
        )
    finally:
        conn.close()
