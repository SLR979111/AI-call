"""Contract tests for Predictive Dialogue API v1."""
import json
from pathlib import Path

from fastapi.testclient import TestClient
from app.main import app

ROOT = Path(__file__).resolve().parents[1]
CLIENT = TestClient(app)
CTX = {"reject_l3": "主体资质到期", "remediation": "更新有效资质后重新提交"}


def test_classify_matches_contract():
    r = CLIENT.post("/predictive/v1/classify", json={
        "operation": "classify", "text": "好", "stage": "REASON", "context": CTX,
    })
    assert r.status_code == 200
    assert r.json()["schema_version"] == "1.0"


def test_prepare_matches_contract():
    r = CLIENT.post("/predictive/v1/prepare", json={
        "operation": "prepare", "stage": "REASON", "context": CTX,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["schema_version"] == "1.0"
    assert body["candidates"]


def test_handle_hit_and_miss_match_contract():
    hit = CLIENT.post("/predictive/v1/handle", json={
        "operation": "handle", "text": "好", "stage": "REASON", "context": CTX,
    })
    assert hit.status_code == 200
    hit_body = hit.json()
    assert hit_body["schema_version"] == "1.0"
    assert hit_body["decision"]["hit"] is True
    assert hit_body["decision"]["next_stage"] == "REMEDIATION"

    miss = CLIENT.post("/predictive/v1/handle", json={
        "operation": "handle", "text": "今天天气不错", "stage": "REASON", "context": CTX,
    })
    assert miss.status_code == 200
    miss_body = miss.json()
    assert miss_body["schema_version"] == "1.0"
    assert miss_body["decision"]["hit"] is False
    assert miss_body["decision"]["filler_used"] is True
    assert miss_body["decision"]["needs_llm"] is True
    assert miss_body["decision"]["candidate"]["source"] == "filler"


def test_blank_text_is_rejected():
    r = CLIENT.post("/predictive/v1/handle", json={
        "operation": "handle", "text": " ", "stage": "REASON", "context": CTX,
    })
    assert r.status_code == 422


def test_contract_endpoint_is_frontend_readable():
    r = CLIENT.get("/predictive/v1/contract")
    assert r.status_code == 200
    body = r.json()
    assert body.get("title") == "Predictive Dialogue API v1"
    assert "definitions" in body
