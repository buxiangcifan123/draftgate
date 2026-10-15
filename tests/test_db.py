"""Tests for the SQLite layer."""

import pytest

from draftgate import config, db


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DRAFTGATE_DB", str(tmp_path / "unit.db"))
    monkeypatch.setenv("DRAFTGATE_TOKEN", "t")
    monkeypatch.setenv("DRAFTGATE_SECRET", "s")
    db.init_db()
    yield


def test_init_creates_tables():
    import sqlite3

    conn = sqlite3.connect(str(config.db_path()))
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()
    assert {"items", "webhook_deliveries"} <= tables


def test_create_and_get_roundtrip():
    item = db.create_item(
        title="T",
        content="C",
        content_type="markdown",
        tags=["a", "b"],
        metadata={"source": "n8n"},
    )
    assert item["status"] == "pending"
    assert item["tags"] == ["a", "b"]
    assert item["metadata"] == {"source": "n8n"}
    fetched = db.get_item(item["id"])
    assert fetched["title"] == "T"
    assert fetched["content"] == "C"


def test_decide_item_transitions():
    item = db.create_item(title="T", content="C")
    decided, delivery_id = db.decide_item(item["id"], "approve", None)
    assert decided["status"] == "approved"
    assert decided["decided_at"] is not None
    assert delivery_id is None  # no callback_url


def test_reason_roundtrip():
    item = db.create_item(title="T", content="C")
    decided, _ = db.decide_item(item["id"], "reject", None, reason="sources missing")
    assert decided["reason"] == "sources missing"
    assert db.get_item(item["id"])["reason"] == "sources missing"


def test_migration_adds_reason_to_existing_db():
    import sqlite3

    config.db_path().unlink()  # drop the fixture's up-to-date database
    conn = sqlite3.connect(str(config.db_path()))
    conn.execute(
        "CREATE TABLE items("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,"
        " content TEXT NOT NULL, final_content TEXT,"
        " content_type TEXT NOT NULL DEFAULT 'text', tags TEXT NOT NULL DEFAULT '[]',"
        " metadata TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'pending',"
        " callback_url TEXT, callback_secret TEXT, created_at TEXT NOT NULL,"
        " decided_at TEXT)"
    )
    conn.execute(
        "INSERT INTO items(title, content, created_at)"
        " VALUES('old', 'body', '2026-01-01 00:00:00')"
    )
    conn.commit()
    conn.close()

    db.init_db()  # must migrate in place instead of failing

    item = db.get_item(1)
    assert item["title"] == "old"
    assert item["reason"] is None


def test_decide_item_conflict():
    item = db.create_item(title="T", content="C")
    db.decide_item(item["id"], "approve", None)
    with pytest.raises(db.ConflictError):
        db.decide_item(item["id"], "reject", None)


def test_decide_item_missing():
    with pytest.raises(db.NotFoundError):
        db.decide_item(999, "approve", None)


def test_decision_creates_delivery_row():
    item = db.create_item(title="T", content="C", callback_url="http://127.0.0.1:9/x")
    _, delivery_id = db.decide_item(item["id"], "reject", None)
    assert delivery_id is not None
    delivery = db.get_delivery_by_item(item["id"])
    assert delivery["status"] == "pending"
    assert delivery["attempts"] == 0


def test_due_deliveries_respects_retry_time():
    item = db.create_item(title="T", content="C", callback_url="http://127.0.0.1:9/x")
    _, delivery_id = db.decide_item(item["id"], "approve", None)
    # fresh delivery has a future next_retry_at -> not due yet
    assert db.due_deliveries(db.utcnow()) == []
    # after the grace window passes it becomes due
    import sqlite3

    conn = sqlite3.connect(str(config.db_path()))
    conn.execute(
        "UPDATE webhook_deliveries SET next_retry_at = '2000-01-01 00:00:00'"
        " WHERE id = ?",
        (delivery_id,),
    )
    conn.commit()
    conn.close()
    due = db.due_deliveries(db.utcnow())
    assert [d["id"] for d in due] == [delivery_id]


def test_list_pending_first():
    first = db.create_item(title="first", content="C")
    second = db.create_item(title="second", content="C")
    db.decide_item(first["id"], "approve", None)
    items = db.list_items()
    assert items[0]["id"] == second["id"]  # pending first
    assert items[1]["id"] == first["id"]


def test_stats_counts():
    a = db.create_item(title="a", content="C")
    b = db.create_item(title="b", content="C")
    db.create_item(title="c", content="C")
    db.decide_item(a["id"], "approve", None)
    db.decide_item(b["id"], "edit", "new")
    assert db.stats() == {"pending": 1, "approved": 1, "rejected": 0, "edited": 1, "total": 3}
