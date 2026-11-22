import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.call_history import CallArchive, router
from app.models import DEFAULT_CARD


def write_session(path, ended_at, *, voice=True, status="complete", messages=(), closed=True, mode="cloud",
                  card=DEFAULT_CARD):
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [{"event": "started", "mode": mode, "voice": voice, "playbook_version": 4,
               "at": ended_at - 10, "task": card.model_dump()}]
    events.extend(messages)
    if closed:
        events.append({"event": "closed", "at": ended_at, "status": status})
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in events), encoding="utf-8")


def test_history_reads_live_archive_and_filters_paged_metadata(tmp_path):
    app = FastAPI()
    app.include_router(router)
    logs = tmp_path / "sessions"
    app.state.call_archive = CallArchive(logs)
    for index in range(1, 23):
        write_session(logs / f"call-{index}.jsonl", index + 10, voice=index % 2 == 0,
                      status="active" if index == 22 else "complete")
    write_session(logs / "ongoing.jsonl", 50, closed=False)
    write_session(tmp_path / "demo" / "sessions" / "example.jsonl", 51)
    (logs / "bad.jsonl").write_text("{invalid json}\n", encoding="utf-8")

    with TestClient(app, base_url="http://127.0.0.1") as client:
        result = client.get("/api/call-history")
        assert result.headers["cache-control"] == "no-store"
        first = result.json()
        assert first["total"] == 22 and len(first["calls"]) == 20
        assert first["calls"][0]["id"] == "call-22"
        assert "transcript" not in first["calls"][0]
        assert "analysis_scope" not in first["calls"][0]
        second = client.get("/api/call-history?page=2").json()
        assert [call["id"] for call in second["calls"]] == ["call-2", "call-1"]
        filtered = client.get("/api/call-history?kind=voice&outcome=active&q=CALL-22").json()
        assert [call["id"] for call in filtered["calls"]] == ["call-22"]
        assert client.get("/api/call-history/ongoing").status_code == 404
        assert client.get("/api/call-history/example").status_code == 404
        assert client.get("/api/call-history/call-1", headers={"Origin": "https://example.test"}).status_code == 403


def test_history_returns_complete_visible_turns_and_analysis_keeps_turn_numbers(tmp_path):
    text = "补交后的材料怎么查？" * 20
    path = tmp_path / "sessions" / "sample.jsonl"
    write_session(path, 300, voice=True, status=None, messages=[
        {"event": "user", "text": text, "at": 292},
        {"event": "prediction_ready", "text": "未使用的候选"},
        {"event": "interrupted", "at": 294, "cutoff": [
            {"text": "可以先打开", "remaining_text": "未播完的部分"},
        ]},
        *[{"event": "delivered", "text": str(index)} for index in range(30)],
    ])
    app = FastAPI()
    app.include_router(router)
    app.state.call_archive = archive = CallArchive(path.parent)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        record = client.get("/api/call-history/sample").json()
    assert record["outcome"] is None and record["version"] == 4
    assert record["analysis_scope"] == {"mode": "cloud"}
    assert len(record["transcript"]) == 32
    assert record["transcript"][:2] == [
        {"role": "caller", "text": text, "at": 292, "turn": 1},
        {"role": "cutoff", "text": "可以先打开", "at": 294, "turn": 2},
    ]
    sample, = archive.calls()
    assert sample.caller_turns == 1 and sample.truncated
    assert len(sample.transcript) == 24 and sample.transcript[0]["text"] == text[:140]
    assert sample.transcript[-1]["turn"] == record["transcript"][-1]["turn"]


def test_legacy_history_optimization_keeps_unmarked_transcripts_local(tmp_path):
    app = FastAPI()
    app.include_router(router)
    app.state.call_archive = CallArchive(tmp_path)
    write_session(tmp_path / "legacy.jsonl", 300, mode=None)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        call = client.get("/api/call-history/legacy").json()
    assert call["mode"] is None
    assert call["analysis_scope"] == {"mode": "local"}
