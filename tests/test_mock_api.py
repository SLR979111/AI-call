from __future__ import annotations

from fastapi.testclient import TestClient

from mock_api.server import IDEMPOTENCY, app


def client() -> TestClient:
    IDEMPOTENCY.clear()
    return TestClient(app)


def test_mock_auth_and_customer_lookup():
    c = client()
    assert c.get("/healthz").status_code == 200
    assert c.get("/v1/customers/CUST-001").status_code == 401
    r = c.get("/v1/customers/CUST-001", headers={"X-API-Key": "dev-mock-key"})
    assert r.status_code == 200
    assert r.json()["customer_id"] == "CUST-001"


def test_mock_order_not_found():
    c = client()
    r = c.get("/v1/orders/UNKNOWN", headers={"X-API-Key": "dev-mock-key"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ORDER_NOT_FOUND"


def test_mock_ticket_idempotency_and_conflict():
    c = client()
    headers = {"X-API-Key": "dev-mock-key", "Idempotency-Key": "t-1"}
    body = {"request_id": "Q983001", "customer_id": "CUST-001",
            "type": "callback", "summary": "回电", "payload": {}}
    first = c.post("/v1/tickets", headers=headers, json=body)
    replay = c.post("/v1/tickets", headers=headers, json=body)
    conflict = c.post("/v1/tickets", headers=headers, json={**body, "summary": "不同"})
    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True
    assert conflict.status_code == 409
