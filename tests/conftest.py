"""Shared fixtures. Each test gets an isolated database via tmp_path."""

import pytest
from fastapi.testclient import TestClient

TEST_TOKEN = "test-token"
TEST_SECRET = "test-secret"


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("DRAFTGATE_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("DRAFTGATE_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("DRAFTGATE_SECRET", TEST_SECRET)
    from draftgate.api import create_app

    app = create_app(start_worker=False)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def auth():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}
