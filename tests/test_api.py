"""Tests for the REST API layer."""

from tests.conftest import TEST_TOKEN


def test_health_no_auth(api):
    resp = api.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_api_requires_token(api):
    resp = api.post("/api/items", json={"title": "t", "content": "c"})
    assert resp.status_code == 401


def test_api_rejects_wrong_token(api):
    resp = api.get(
        "/api/items", headers={"Authorization": "Bearer wrong-token"}
    )
    assert resp.status_code == 401


def test_api_accepts_x_draftgate_token_header(api):
    resp = api.get("/api/stats", headers={"X-Draftgate-Token": TEST_TOKEN})
    assert resp.status_code == 200


def test_submit_validation(api, auth):
    assert api.post("/api/items", json={"title": "", "content": "c"}, headers=auth).status_code == 422
    assert api.post("/api/items", json={"title": "t"}, headers=auth).status_code == 422
    bad = {"title": "t", "content": "c", "content_type": "bogus"}
    assert api.post("/api/items", json=bad, headers=auth).status_code == 422


def test_submit_and_get_detail(api, auth):
    resp = api.post(
        "/api/items",
        json={
            "title": "Weekly digest",
            "content": "# hello",
            "content_type": "markdown",
            "tags": ["news"],
            "metadata": {"source": "n8n"},
        },
        headers=auth,
    )
    assert resp.status_code == 201
    item = resp.json()
    assert item["status"] == "pending"
    assert item["content"] == "# hello"
    assert item["tags"] == ["news"]

    detail = api.get(f"/api/items/{item['id']}", headers=auth).json()
    assert detail["metadata"] == {"source": "n8n"}
    assert detail["webhook"] is None


def test_list_preview_truncated(api, auth):
    api.post("/api/items", json={"title": "t", "content": "x" * 500}, headers=auth)
    listed = api.get("/api/items", headers=auth).json()
    assert len(listed[0]["content_preview"]) == 203  # 200 + "..."
    detail = api.get(f"/api/items/{listed[0]['id']}", headers=auth).json()
    assert len(detail["content"]) == 500


def test_list_invalid_status_filter(api, auth):
    assert api.get("/api/items?status=bogus", headers=auth).status_code == 422


def test_decision_flow_approve(api, auth):
    item = api.post("/api/items", json={"title": "t", "content": "c"}, headers=auth).json()
    resp = api.post(
        f"/api/items/{item['id']}/decision", json={"action": "approve"}, headers=auth
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    assert resp.json()["final_content"] is None


def test_decision_flow_reject(api, auth):
    item = api.post("/api/items", json={"title": "t", "content": "c"}, headers=auth).json()
    resp = api.post(
        f"/api/items/{item['id']}/decision", json={"action": "reject"}, headers=auth
    )
    assert resp.json()["status"] == "rejected"


def test_decision_records_reason(api, auth):
    item = api.post("/api/items", json={"title": "t", "content": "c"}, headers=auth).json()
    resp = api.post(
        f"/api/items/{item['id']}/decision",
        json={"action": "reject", "reason": "  too promotional  "},
        headers=auth,
    )
    assert resp.status_code == 200
    assert resp.json()["reason"] == "too promotional"  # trimmed

    detail = api.get(f"/api/items/{item['id']}", headers=auth).json()
    assert detail["reason"] == "too promotional"


def test_decision_reason_is_optional(api, auth):
    item = api.post("/api/items", json={"title": "t", "content": "c"}, headers=auth).json()
    body = api.post(
        f"/api/items/{item['id']}/decision", json={"action": "approve"}, headers=auth
    ).json()
    assert body["reason"] is None


def test_blank_reason_is_stored_as_null(api, auth):
    item = api.post("/api/items", json={"title": "t", "content": "c"}, headers=auth).json()
    body = api.post(
        f"/api/items/{item['id']}/decision",
        json={"action": "edit", "content": "fixed", "reason": "   "},
        headers=auth,
    ).json()
    assert body["status"] == "edited"
    assert body["reason"] is None


def test_decision_flow_edit(api, auth):
    item = api.post("/api/items", json={"title": "t", "content": "c"}, headers=auth).json()
    resp = api.post(
        f"/api/items/{item['id']}/decision",
        json={"action": "edit", "content": "fixed by human"},
        headers=auth,
    )
    body = resp.json()
    assert body["status"] == "edited"
    assert body["final_content"] == "fixed by human"
    assert body["content"] == "c"  # original kept


def test_decision_edit_requires_content(api, auth):
    item = api.post("/api/items", json={"title": "t", "content": "c"}, headers=auth).json()
    resp = api.post(
        f"/api/items/{item['id']}/decision", json={"action": "edit"}, headers=auth
    )
    assert resp.status_code == 422


def test_decision_twice_returns_409(api, auth):
    item = api.post("/api/items", json={"title": "t", "content": "c"}, headers=auth).json()
    api.post(f"/api/items/{item['id']}/decision", json={"action": "approve"}, headers=auth)
    resp = api.post(
        f"/api/items/{item['id']}/decision", json={"action": "approve"}, headers=auth
    )
    assert resp.status_code == 409


def test_decision_missing_item_404(api, auth):
    assert (
        api.post("/api/items/999/decision", json={"action": "approve"}, headers=auth).status_code
        == 404
    )


def test_stats_endpoint(api, auth):
    api.post("/api/items", json={"title": "a", "content": "c"}, headers=auth)
    api.post("/api/items", json={"title": "b", "content": "c"}, headers=auth)
    stats = api.get("/api/stats", headers=auth).json()
    assert stats["pending"] == 2
    assert stats["total"] == 2


def test_ui_served_without_token(api):
    resp = api.get("/")
    assert resp.status_code == 200
    assert "DraftGate" in resp.text


def test_cors_locked_to_localhost(api):
    resp = api.options(
        "/api/items",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"
    resp = api.options(
        "/api/items",
        headers={
            "Origin": "http://evil.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.headers.get("access-control-allow-origin") is None
