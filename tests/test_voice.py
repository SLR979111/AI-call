from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import threading
import time
import sys
import subprocess
from pathlib import Path
from types import SimpleNamespace
from contextlib import aclosing

import pytest
from fastapi.testclient import TestClient

from app.cancel import CancelToken, TurnCancelled
from app.main import app
from app.voice.factory import VoiceRuntime, create_tts, providers
from app.voice.fake import FakeAsr, FakeAsrStream, FakeTts
from app.voice.qwen import QwenTts
from app.voice.sherpa_asr import SherpaAsrStream
from app.voice.types import AsrEvent, AudioChunk, AudioFrame, VoiceError, mono_ms
from services.qwen_tts.server import create_app, QwenEngine, INSTRUCT


def test_provider_defaults_and_legacy(monkeypatch):
    for key in ("ASR_PROVIDER", "TTS_PROVIDER", "VOICE_PROVIDER", "TTS_FALLBACK_PROVIDER"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("APP_ENV", "prod")
    assert providers() == ("sherpa", "qwen3")
    assert isinstance(create_tts(), QwenTts)  # no model imports / Melo required
    monkeypatch.setenv("VOICE_PROVIDER", "sherpa-onnx")
    assert providers() == ("sherpa", "melo")
    monkeypatch.setenv("TTS_PROVIDER", "fake")
    assert providers() == ("sherpa", "fake")
    monkeypatch.setenv("VOICE_PROVIDER", "fake")
    assert providers() == ("fake", "fake")
    monkeypatch.setenv("TTS_FALLBACK_PROVIDER", "melo")
    with pytest.raises(VoiceError, match="fallback"):
        create_tts()


def frame(seq=0):
    return struct.pack(">BBHHIIH", 0xA1, 1, 16000, 320, seq, 123, 0) + b"\0" * 640


def test_pcm_frame_validation():
    parsed = AudioFrame.parse(frame(9))
    assert (parsed.seq, parsed.client_mono_ms, parsed.sample_rate, len(parsed.pcm)) == (9, 123, 16000, 640)
    for data in (b"", frame()[:-1], b"\xA2" + frame()[1:], frame()[:2] + b"\0\0" + frame()[4:]):
        with pytest.raises(VoiceError):
            AudioFrame.parse(data)


async def test_fake_audio_contract_and_cancel():
    token = CancelToken()
    chunks = [c async for c in FakeTts().synthesize("test", sample_rate=16000, cancel=token)]
    assert sum(len(c.pcm) for c in chunks) == 3200
    assert chunks[-1].is_last and chunks[0].ts_mono_ms > 0
    token.cancel()
    with pytest.raises(TurnCancelled):
        await anext(FakeTts().synthesize("test", cancel=token))


async def test_qwen_ws_pcm_and_close():
    from websockets.asyncio.server import serve
    closed = asyncio.Event()
    async def handler(ws):
        request = json.loads(await ws.recv())
        assert request == {"text": "您好", "sample_rate": 24000}
        await ws.send(json.dumps({"type": "started", "sample_rate": 24000, "channels": 1}))
        await ws.send(b"\x01\0" * 1200)
        await ws.send(b"\x02\0" * 1200)
        await ws.send(json.dumps({"type": "completed", "samples": 2400}))
        await ws.wait_closed()
        closed.set()
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = QwenTts(f"http://127.0.0.1:{port}")
        chunks = [c async for c in client.synthesize("您好", cancel=CancelToken())]
        assert [c.seq for c in chunks] == [0, 1, 2]
        assert chunks[-1].is_last and sum(len(c.pcm) for c in chunks) == 4800
        await asyncio.wait_for(closed.wait(), 1)


async def test_qwen_cancel_while_waiting_closes_socket():
    from websockets.asyncio.server import serve
    connected, closed = asyncio.Event(), asyncio.Event()
    async def handler(ws):
        await ws.recv()
        connected.set()
        await ws.wait_closed()
        closed.set()
    async with serve(handler, "127.0.0.1", 0) as server:
        client = QwenTts(f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        token = CancelToken()
        stream = client.synthesize("您好", cancel=token)
        task = asyncio.create_task(anext(stream))
        await connected.wait()
        start = time.perf_counter()
        token.cancel("BARGE_IN")
        with pytest.raises(TurnCancelled):
            await asyncio.wait_for(task, 1)
        await asyncio.wait_for(closed.wait(), 1)
        assert time.perf_counter() - start < 1


@pytest.mark.parametrize("response", [
    {"type": "started", "sample_rate": 44100, "channels": 1},
    {"type": "error", "error": {"code": "TTS_BUSY", "message": "busy", "retryable": True}},
    {"type": "completed", "samples": 0},
])
async def test_qwen_errors_are_explicit(response):
    from websockets.asyncio.server import serve
    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps(response))
        await ws.wait_closed()
    async with serve(handler, "127.0.0.1", 0) as server:
        client = QwenTts(f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        with pytest.raises(VoiceError):
            await anext(client.synthesize("您好", cancel=CancelToken()))


async def test_qwen_status_unavailable_no_fallback(monkeypatch):
    monkeypatch.setenv("ASR_PROVIDER", "fake")
    monkeypatch.setenv("TTS_PROVIDER", "qwen3")
    monkeypatch.setenv("QWEN_TTS_URL", "http://127.0.0.1:1")
    runtime = VoiceRuntime()
    await runtime.initialize()
    status = (await runtime.refresh()).to_dict()
    assert status["asr"]["ready"] and not status["tts"]["ready"]
    assert status["tts"]["provider"] == "qwen3" and isinstance(runtime.tts, QwenTts)


class MockEngine:
    sample_rate = 24000

    def __init__(self, endless=False):
        self.ready = True
        self.detail = "test"
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.endless = endless

    def load(self):
        pass

    def generate(self, text):
        try:
            for _ in range(10000 if self.endless else 3):
                time.sleep(0.005)
                yield b"\x01\0" * 240
        finally:
            self.closed.set()


def test_sidecar_protocol_and_server_owned_voice():
    engine = MockEngine()
    with TestClient(create_app(engine)) as client:
        assert client.get("/healthz").json()["ready"]
        with client.websocket_connect("/ws/tts") as ws:
            ws.send_json({"text": "您好", "sample_rate": 24000})
            assert ws.receive_json()["sample_rate"] == 24000
            assert sum(len(ws.receive_bytes()) for _ in range(3)) == 1440
            assert ws.receive_json()["samples"] == 720
        assert engine.closed.wait(1) and not engine.lock.locked()
        with client.websocket_connect("/ws/tts") as ws:
            ws.send_json({"text": "您好", "ref_audio": "untrusted.wav"})
            assert ws.receive_json()["error"]["code"] == "TTS_INVALID_REQUEST"
        engine.ready = False
        assert client.get("/healthz").status_code == 503


def test_sidecar_disconnect_releases_generation_and_busy():
    engine = MockEngine(endless=True)
    with TestClient(create_app(engine)) as client:
        with client.websocket_connect("/ws/tts") as ws:
            ws.send_json({"text": "您好"})
            ws.receive_json()
            ws.receive_bytes()
            with client.websocket_connect("/ws/tts") as second:
                second.send_json({"text": "您好"})
                assert second.receive_json()["error"]["code"] == "TTS_BUSY"
        assert engine.closed.wait(1)
        assert not engine.lock.locked()


class Recognizer:
    def __init__(self, slow=False):
        self.slow = slow
        self.pending = False

    def create_stream(self):
        return self

    def accept_waveform(self, rate, samples):
        self.pending = True

    def input_finished(self):
        pass

    def is_ready(self, stream):
        return self.pending

    def decode_stream(self, stream):
        if self.slow:
            time.sleep(0.4)
        self.pending = False

    def get_result(self, stream):
        return "您好"

    def is_endpoint(self, stream):
        return False


@pytest.mark.parametrize("slow", [False, True])
async def test_asr_finish_final_or_error_within_300ms(slow):
    stream = SherpaAsrStream(Recognizer(slow))
    start = time.perf_counter()
    await stream.finish()
    events = [e async for e in stream.events()]
    assert time.perf_counter() - start < 0.3
    assert events[-1].kind == ("error" if slow else "final")


async def test_contest_asr_waits_for_the_browser_endpoint():
    class EagerEndpoint(Recognizer):
        def is_endpoint(self, stream):
            pytest.fail("a native endpoint must not split a browser-managed turn")

    stream = SherpaAsrStream(EagerEndpoint(), client_endpointing=True)
    await stream.push(AudioFrame.parse(frame()))
    await stream.push(AudioFrame.parse(frame(1)))
    await stream.finish()
    assert [(event.kind, event.text) async for event in stream.events()] == [
        ("partial", "您好"), ("final", "您好"),
    ]


async def test_melo_new_reply_waits_for_cancelled_native_generation():
    pytest.importorskip("scipy")
    from app.voice.melo import MeloTts

    old_started = threading.Event()
    second_started = threading.Event()
    release_old = threading.Event()
    seen = []

    class NativeModel:
        def generate(self, text, *, sid, speed, callback):
            seen.append(text)
            if text == "旧回复":
                old_started.set()
                release_old.wait(1)
                assert callback([0.5], 1) == 0
            else:
                second_started.set()
                assert callback([0.5], 1) == 1

    tts = MeloTts.__new__(MeloTts)
    tts.speed = 1.0
    tts.lock = threading.Lock()
    tts.model = NativeModel()

    async def generate(text, cancel):
        return [chunk async for chunk in tts.synthesize(text, sample_rate=44100, cancel=cancel)]

    cancel = CancelToken()
    old = asyncio.create_task(generate("旧回复", cancel))
    try:
        # Wait for the old generation to start (increased timeout for slow CI)
        started = await asyncio.to_thread(old_started.wait, 5)
        assert started, "Old generation thread did not start within 5 seconds"
        cancel.cancel()
        with pytest.raises(TurnCancelled):
            await old
        new = asyncio.create_task(generate("AI客服的APP", CancelToken()))
        await asyncio.sleep(0.08)
        assert not new.done()
        release_old.set()
        chunks = await asyncio.wait_for(new, 1)
        assert second_started.is_set()
        assert seen == ["旧回复", "人工智能客服的应用"]
        assert chunks[0].pcm and chunks[-1].is_last
    finally:
        release_old.set()


def test_app_audio_duration_timing_and_invalid_uplink():
    with TestClient(app) as client:
        sid = client.post("/sessions", json={"mode": "voice"}).json()["session_id"]
        with client.websocket_connect(f"/ws/session/{sid}") as ws:
            ws.receive_json(); ws.receive_json()
            ws.send_bytes(b"bad")
            assert ws.receive_json()["error"]["code"] == "INVALID_AUDIO_FRAME"
            ws.send_json({"type": "text", "text": "退款多久到账"})
            messages = []
            while True:
                msg = ws.receive_json()
                messages.append(msg)
                if msg["type"] == "audio_chunk":
                    ws.send_json({"type": "playback_first_frame", "turn_id": msg["turn_id"],
                                  "reply_id": msg["reply_id"]})
                if msg["type"] == "turn_result":
                    break
            chunks = [m for m in messages if m["type"] == "audio_chunk"]
            assert chunks and [c["seq"] for c in chunks] == list(range(len(chunks)))
            started = next(m for m in messages if m["type"] == "tts_started")
            assert started["audio_format"] == "pcm16"
            duration = sum(len(base64.b64decode(c["pcm_b64"])) / 2 for c in chunks) * 1000 / started["sample_rate"]
            completed = next(m for m in messages if m["type"] == "tts_completed")
            assert completed["total_ms"] == pytest.approx(duration, abs=0.01)
            # A ping yields to the turn task so the latency event is persisted.
            ws.send_json({"type": "ping"})
            while ws.receive_json()["type"] != "pong":
                pass
            events = client.app.state.bus.read_events(sid)
            timing = next(e["payload"] for e in events if e["type"] == "TurnLatency")
            assert timing["t7_tts_first_chunk_ms"] <= timing["t8_first_downlink_ms"] <= timing["t9_first_playback_ms"]
            assert timing["playback_measured"]


@pytest.mark.parametrize("endpoint", [False, True])
def test_app_asr_final_routes_once_to_turn(endpoint):
    class Stream(FakeAsrStream):
        async def push(self, frame):
            assert len(frame.pcm) == 640
            self.queue.put_nowait(AsrEvent("partial", "退款"))
            if endpoint:
                self.queue.put_nowait(AsrEvent("final", "退款多久到账", confidence=0.8, audio_end_ms=20))

        async def finish(self):
            self.queue.put_nowait(AsrEvent("final", "" if endpoint else "退款多久到账", confidence=0.8, audio_end_ms=20))
            await self.close()
    class Asr(FakeAsr):
        async def start(self, *args, **kwargs):
            return Stream()
    with TestClient(app) as client:
        client.app.state.voice.asr = Asr()
        sid = client.post("/sessions", json={"mode": "voice"}).json()["session_id"]
        previous = client.post(f"/sessions/{sid}/text", json={"text": "你好"}).json()
        with client.websocket_connect(f"/ws/session/{sid}") as ws:
            ws.receive_json(); ws.receive_json()
            ws.send_bytes(frame())
            assert ws.receive_json()["final"] is False
            ws.send_json({"type": "speech_ended"})
            finals = []
            while True:
                msg = ws.receive_json()
                if msg["type"] == "transcript" and msg.get("final"):
                    finals.append(msg)
                if msg["type"] == "turn_result":
                    break
            assert len(finals) == 1
            assert finals[0]["turn_id"] == msg["turn_id"] != previous["turn_id"]
            assert finals[0]["confidence"] == 0.8 and finals[0]["audio_end_ms"] == 20
            assert client.get(f"/sessions/{sid}/state").json()["turn_seq"] == 2


def test_app_cancel_stops_old_audio_and_worker():
    closed = threading.Event()
    class SlowTts:
        sample_rate = 24000
        async def synthesize(self, text, *, cancel, **kwargs):
            try:
                for i in range(100):
                    cancel.check()
                    yield AudioChunk(b"\x01\0" * 240, i, ts_mono_ms=mono_ms())
                    await asyncio.sleep(0.02)
            finally:
                closed.set()
    with TestClient(app) as client:
        client.app.state.orchestrator.tts = SlowTts()
        sid = client.post("/sessions", json={"mode": "voice"}).json()["session_id"]
        with client.websocket_connect(f"/ws/session/{sid}") as ws:
            ws.receive_json(); ws.receive_json()
            ws.send_json({"type": "text", "text": "你好"})
            while ws.receive_json()["type"] != "audio_chunk":
                pass
            ws.send_json({"type": "speech_started"})
            cancelled = False
            while True:
                msg = ws.receive_json()
                if cancelled:
                    assert msg["type"] != "audio_chunk"
                cancelled |= msg["type"] == "cancel"
                if msg["type"] == "tts_completed":
                    assert msg["interrupted"]
                if msg["type"] == "turn_aborted":
                    break
            assert cancelled and closed.wait(1)


@pytest.mark.parametrize("termination", ["pcm", "empty", "missing"])
def test_qwen_grouping_preserves_sentences_and_honest_alignment(termination):
    calls = []
    class Agent:
        async def handle(self, req, cancel, tools):
            for index, text in enumerate(["第一句。", "第二句。", "第三句。", "第四句。", "第五句。"]):
                yield {"kind": "sentence", "text": text, "final": index == 4}
            yield {"kind": "end", "outcome": "completed"}
    class Tts(FakeTts):
        async def synthesize(self, text, **kwargs):
            calls.append(text)
            async for chunk in super().synthesize(text, **kwargs):
                chunk.is_last = termination == "pcm"
                yield chunk
            if termination == "empty":
                yield AudioChunk(b"", 1, is_last=True, ts_mono_ms=mono_ms())
    with TestClient(app) as client:
        orc = client.app.state.orchestrator
        orc.agent, orc.tts, orc.tts_group_size = Agent(), Tts(), 3
        orc.tts_provider, orc.tts_alignment = "qwen3", False
        sid = client.post("/sessions", json={"mode": "voice"}).json()["session_id"]
        with client.websocket_connect(f"/ws/session/{sid}") as ws:
            ws.receive_json(); ws.receive_json()
            ws.send_json({"type": "text", "text": "你好"})
            messages = []
            while True:
                msg = ws.receive_json()
                messages.append(msg)
                if msg["type"] == "turn_result":
                    break
            assert calls == ["第一句。", "第二句。第三句。第四句。", "第五句。"]
            provisional = [m for m in messages if m["type"] == "tts_sentence" and m["provisional"]]
            assert [m["sentence_id"] for m in provisional] == [1, 2, 3, 4, 5]
            assert all(m["estimated"] and not m["tts_alignment"] for m in provisional)
            audio = [m for m in messages if m["type"] == "audio_chunk"]
            assert [m["seq"] for m in audio] == list(range(6))
            assert [m["is_last"] for m in audio] == [False, True] * 3
            assert all(not m["pcm_b64"] for m in audio if m["is_last"])
            for chunk in audio:
                for number in chunk["sentence_ids"]:
                    meta = next(m for m in provisional if m["sentence_id"] == number)
                    assert messages.index(meta) < messages.index(chunk)
                    assert meta["reply_id"] == chunk["reply_id"]
            sentences = [m for m in messages if m["type"] == "tts_sentence" and not m["provisional"]]
            assert [m["sentence_id"] for m in sentences] == [1, 2, 3, 4, 5]
            assert [m["estimated"] for m in sentences] == [False, True, True, True, False]
            assert sentences[0]["start_ms"] == 0 and sentences[-1]["end_ms"] == 300
            assert [m["char_start"] for m in sentences] == [0, 4, 8, 12, 16]
            assert next(m for m in messages if m["type"] == "tts_completed")["total_ms"] == 300
            ws.send_json({"type": "ping"})
            while ws.receive_json()["type"] != "pong":
                pass
            timing = next(e["payload"] for e in client.app.state.bus.read_events(sid) if e["type"] == "TurnLatency")
            assert timing["t9_first_playback_ms"] is None and timing["e2e_ms"] is None


@pytest.mark.parametrize("after_pcm", [False, True])
def test_app_tts_failure_is_terminal_and_not_fallback(after_pcm):
    class Broken:
        sample_rate = 24000
        async def synthesize(self, *args, **kwargs):
            if after_pcm:
                yield AudioChunk(b"\x01\0" * 240, 0)
            raise VoiceError("QWEN_OUTPUT_LIMIT" if after_pcm else "QWEN_UNAVAILABLE", "test failure", False)
    with TestClient(app) as client:
        client.app.state.orchestrator.tts = Broken()
        sid = client.post("/sessions", json={"mode": "voice"}).json()["session_id"]
        with client.websocket_connect(f"/ws/session/{sid}") as ws:
            ws.receive_json(); ws.receive_json()
            ws.send_json({"type": "text", "text": "你好"})
            messages = []
            while True:
                msg = ws.receive_json()
                messages.append(msg)
                if msg["type"] == "turn_result":
                    assert msg["result"]["outcome"] == "error"
                    break
            audio = [m for m in messages if m["type"] == "audio_chunk"]
            assert len(audio) == int(after_pcm) and not any(m["is_last"] for m in audio)
            assert next(m for m in messages if m["type"] == "error")["error"]["code"] == (
                "QWEN_OUTPUT_LIMIT" if after_pcm else "QWEN_UNAVAILABLE")
            if after_pcm:
                completed = next(m for m in messages if m["type"] == "tts_completed")
                assert completed["interrupted"] and completed["total_ms"] == 10


@pytest.fixture
def stub_qwen_load(monkeypatch, tmp_path):
    # Model-free seam at the external inference library; exercise load + actual
    # generate argument forwarding, including warmup, without downloading files.
    for name in ("QWEN_NON_STREAMING_MODE", "QWEN_TEMPERATURE", "QWEN_TOP_K", "QWEN_TOP_P",
                 "QWEN_CHUNK_SIZE", "QWEN_MAX_NEW_TOKENS", "QWEN_INSTRUCT"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "config.json").write_text(json.dumps({"tts_model_type": "base", "tts_model_size": "1b7",
                                                    "tokenizer_type": "qwen3_tts_tokenizer_12hz"}))
    ref = tmp_path / "reference-placeholder.txt"
    ref.write_text("not audio; mock does not decode")
    for name, value in {"QWEN_MODEL_DIR": str(tmp_path), "QWEN_REF_AUDIO": str(ref),
                        "QWEN_REF_TEXT": "测试参考", "QWEN_REFERENCE_CONSENT": "1"}.items():
        monkeypatch.setenv(name, value)
    calls = []
    class Model:
        talker_graph = SimpleNamespace(cache_position=SimpleNamespace(item=lambda: 100), max_seq_len=2048)
        def warmup(self, **kwargs):
            pass
        def generate_voice_clone_streaming(self, **kwargs):
            calls.append(kwargs)
            yield [0.1] * 240, 24000, {"total_steps_so_far": 1}
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(bfloat16="bf16"))
    monkeypatch.setitem(sys.modules, "faster_qwen3_tts", SimpleNamespace(
        FasterQwen3TTS=SimpleNamespace(from_pretrained=lambda *a, **kw: Model())))
    return QwenEngine(), calls


def test_qwen_audition_defaults_and_health(stub_qwen_load):
    engine, calls = stub_qwen_load
    engine.load()
    list(engine.generate("验收"))
    expected = dict(language="Chinese", non_streaming_mode=False, temperature=0.9, top_k=50, top_p=1.0,
                    chunk_size=4, instruct=INSTRUCT, xvec_only=False, max_new_tokens=2048)
    assert engine.generation_settings == expected
    assert len(calls) == 2
    assert all({key: call[key] for key in expected} == expected for call in calls)
    with TestClient(create_app(engine)) as client:
        assert client.get("/healthz").json()["generation"] == expected


def test_qwen_generation_env_overrides(stub_qwen_load, monkeypatch):
    engine, calls = stub_qwen_load
    for key, value in {"NON_STREAMING_MODE": "true", "TEMPERATURE": "0.7", "TOP_K": "40",
                       "TOP_P": "0.8", "CHUNK_SIZE": "2", "MAX_NEW_TOKENS": "128",
                       "INSTRUCT": "测试指令"}.items():
        monkeypatch.setenv("QWEN_" + key, value)
    engine.load()
    assert {k: calls[0][k] for k in engine.generation_settings} == dict(
        language="Chinese", non_streaming_mode=True, temperature=0.7, top_k=40, top_p=0.8,
        chunk_size=2, max_new_tokens=128, instruct="测试指令", xvec_only=False)


@pytest.mark.parametrize("key,value", [("NON_STREAMING_MODE", "maybe"), ("TEMPERATURE", "0"),
    ("TEMPERATURE", "nan"), ("TOP_K", "0"), ("TOP_P", "1.1"), ("CHUNK_SIZE", "0"), ("MAX_NEW_TOKENS", "0")])
def test_qwen_invalid_generation_settings(stub_qwen_load, monkeypatch, key, value):
    engine, calls = stub_qwen_load
    monkeypatch.setenv("QWEN_" + key, value)
    with pytest.raises(VoiceError) as error:
        engine.load()
    assert error.value.code == "QWEN_CONFIG" and not calls


@pytest.mark.parametrize("limit", ["tokens", "context"])
def test_qwen_output_limits_do_not_report_success(stub_qwen_load, limit):
    engine, _ = stub_qwen_load
    engine.load()
    closed = []
    def generate(**kwargs):
        try:
            yield [0.1] * 240, 24000, {"total_steps_so_far": 1}
            yield [0.1] * 240, 24000, {"total_steps_so_far": engine.max_new_tokens if limit == "tokens" else 2}
        finally:
            closed.append(True)
    engine.model.generate_voice_clone_streaming = generate
    if limit == "context":
        engine.model.talker_graph.cache_position = SimpleNamespace(item=lambda: 2046)
    engine.load = lambda: None
    with TestClient(create_app(engine)) as client:
        with client.websocket_connect("/ws/tts") as ws:
            ws.send_json({"text": "长文本"})
            assert ws.receive_json()["type"] == "started"
            assert ws.receive_bytes()
            if limit == "context":
                assert ws.receive_bytes()
            message = ws.receive_json()
            assert message["type"] == "error" and message["error"]["code"] == "QWEN_OUTPUT_LIMIT"
            assert not message["error"]["retryable"]
    assert closed and not engine.lock.locked()


@pytest.mark.parametrize("text", ["", "  ", "长" * 2001])
async def test_qwen_invalid_text_rejected_before_connect(text):
    with pytest.raises(VoiceError) as error:
        await anext(QwenTts("http://127.0.0.1:1").synthesize(text, cancel=CancelToken()))
    assert error.value.code == "TTS_INVALID_REQUEST" and not error.value.retryable


def test_sidecar_text_length_boundary():
    with TestClient(create_app(MockEngine())) as client:
        for length in (2000, 2001):
            with client.websocket_connect("/ws/tts") as ws:
                ws.send_json({"text": "字" * length})
                message = ws.receive_json()
                if length == 2001:
                    assert message["error"]["code"] == "TTS_INVALID_REQUEST"
                else:
                    assert message["type"] == "started"
                    for _ in range(3):
                        ws.receive_bytes()
                    assert ws.receive_json()["type"] == "completed"


@pytest.mark.parametrize("lengths,expected", [([1, 1000, 1000, 1], [1, 2000, 1]),
    ([1, 1200, 1200], [1, 1200, 1200]), ([2000], [2000]), ([2001], [])])
def test_qwen_long_groups_keep_sentence_boundaries(lengths, expected):
    calls = []
    texts = ["字" * length for length in lengths]
    class Agent:
        async def handle(self, *args):
            for text in texts:
                yield {"kind": "sentence", "text": text}
            yield {"kind": "end", "outcome": "completed"}
    class Tts(FakeTts):
        async def synthesize(self, text, **kwargs):
            calls.append(text)
            async for chunk in super().synthesize(text, **kwargs):
                yield chunk
    with TestClient(app) as client:
        orc = client.app.state.orchestrator
        orc.agent, orc.tts, orc.tts_provider, orc.tts_group_size = Agent(), Tts(), "qwen3", 3
        sid = client.post("/sessions", json={"mode": "voice"}).json()["session_id"]
        with client.websocket_connect(f"/ws/session/{sid}") as ws:
            ws.receive_json(); ws.receive_json()
            ws.send_json({"type": "text", "text": "你好"})
            messages = []
            while True:
                message = ws.receive_json()
                messages.append(message)
                if message["type"] == "turn_result":
                    break
            assert [len(text) for text in calls] == expected
            if expected:
                assert "".join(calls) == "".join(texts)
                sentences = [m for m in messages if m["type"] == "tts_sentence" and not m["provisional"]]
                assert [m["text"] for m in sentences] == texts
                assert sentences[-1]["char_end"] == sum(lengths)
            else:
                assert message["result"]["error"]["code"] == "TTS_INVALID_REQUEST"


def test_default_application_does_not_import_torch_or_melo():
    env = {k: v for k, v in os.environ.items() if k not in
           ("VOICE_PROVIDER", "TTS_PROVIDER", "ASR_PROVIDER", "TTS_FALLBACK_PROVIDER")}
    env["APP_ENV"] = "prod"
    subprocess.run([sys.executable, "-c", "import sys; import app.main; "
                    "from app.voice.factory import create_tts; create_tts(); "
                    "assert 'torch' not in sys.modules; assert 'app.voice.melo' not in sys.modules"],
                   env=env, check=True, timeout=15)


def test_cancel_after_synthesis_while_client_still_playing():
    with TestClient(app) as client:
        sid = client.post("/sessions", json={"mode": "voice"}).json()["session_id"]
        with client.websocket_connect(f"/ws/session/{sid}") as ws:
            ws.receive_json(); ws.receive_json()
            ws.send_json({"type": "text", "text": "你好"})
            while ws.receive_json()["type"] != "turn_result":
                pass
            ws.send_json({"type": "speech_started"})
            assert ws.receive_json()["type"] == "cancel"
            completed = ws.receive_json()
            assert completed["type"] == "tts_completed" and completed["interrupted"]


@pytest.mark.integration
async def test_real_melo_optional():
    if not os.getenv("MELO_MODEL_DIR"):
        pytest.skip("set MELO_MODEL_DIR to a local model")
    model = create_tts("melo")
    for rate in (44100, 24000):
        chunks = [c async for c in model.synthesize("您好，请问明天下午方便吗？", sample_rate=rate, cancel=CancelToken())]
        assert chunks[-1].is_last and all(c.sample_rate == rate for c in chunks)
        assert sum(len(c.pcm) for c in chunks) > rate


@pytest.mark.integration
async def test_real_asr_optional():
    if not os.getenv("ASR_MODEL_DIR"):
        pytest.skip("set ASR_MODEL_DIR to a local model")
    from app.voice.factory import create_asr
    stream = await create_asr("sherpa").start("test")
    await stream.push(AudioFrame.parse(frame()))
    start = time.perf_counter()
    await stream.finish()
    assert time.perf_counter() - start < 0.3
    assert [e async for e in stream.events()][-1].kind in ("final", "error")


@pytest.mark.integration
async def test_real_qwen_optional():
    if not os.getenv("VOICE_TEST_QWEN_URL"):
        pytest.skip("set VOICE_TEST_QWEN_URL to a ready sidecar")
    chunks = [c async for c in QwenTts(os.environ["VOICE_TEST_QWEN_URL"]).synthesize("您好。", cancel=CancelToken())]
    assert chunks[-1].is_last and sum(len(c.pcm) for c in chunks) > 4800


@pytest.mark.integration
@pytest.mark.parametrize("provider", ["melo", "qwen3"])
def test_real_application_asgi_voice_optional(provider, monkeypatch):
    """Real models, in-process ASGI WebSocket; no listening application port."""
    if not os.getenv("ASR_MODEL_DIR") or not os.getenv(
            "MELO_MODEL_DIR" if provider == "melo" else "VOICE_TEST_QWEN_URL"):
        pytest.skip("configure local ASR and selected TTS for application integration")
    monkeypatch.setenv("ASR_PROVIDER", "sherpa")
    monkeypatch.setenv("TTS_PROVIDER", provider)
    monkeypatch.setenv("QWEN_TTS_URL", os.getenv("VOICE_TEST_QWEN_URL", "http://127.0.0.1:8090"))
    result = {"provider": provider, "transport": "in-process ASGI WebSocket (TestClient)",
              "playback_measured": False, "input_mode": "unpaced generated speech"}
    with TestClient(app) as client:
        result["status"] = client.get("/voice/status").json()
        assert result["status"]["asr_ready"] and result["status"]["tts_ready"]
        session = client.post("/sessions", json={"mode": "voice"}).json()
        sid = session["session_id"]
        with client.websocket_connect(session["ws_url"]) as ws:
            ws.receive_json(); ws.receive_json()
            def collect(start):
                messages, first = [], None
                while True:
                    msg = ws.receive_json()
                    messages.append(msg)
                    assert msg["type"] != "error", msg
                    if msg["type"] == "audio_chunk" and msg["pcm_b64"] and first is None:
                        first = (time.perf_counter() - start) * 1000
                    if msg["type"] == "turn_result":
                        break
                started = next(m for m in messages if m["type"] == "tts_started")
                audio = [m for m in messages if m["type"] == "audio_chunk"]
                pcm = b"".join(base64.b64decode(m["pcm_b64"]) for m in audio)
                assert pcm and any(pcm)
                assert [m["seq"] for m in audio] == list(range(len(audio)))
                groups = {}
                for chunk in audio:
                    groups.setdefault(tuple(chunk["sentence_ids"]), []).append(chunk)
                for ids, chunks in groups.items():
                    assert len(chunks) > 1 and chunks[-1]["is_last"] and not chunks[-1]["pcm_b64"]
                    assert all(not m["is_last"] for m in chunks[:-1])
                    for number in ids:
                        metadata = next(m for m in messages if m["type"] == "tts_sentence" and m["sentence_id"] == number)
                        assert messages.index(metadata) < messages.index(chunks[0])
                duration = len(pcm) / 2 / started["sample_rate"] * 1000
                completed = next(m for m in messages if m["type"] == "tts_completed"
                                 and m["reply_id"] == started["reply_id"])
                assert not completed["interrupted"] and completed["total_ms"] == pytest.approx(duration, abs=0.01)
                finals = [m for m in messages if m["type"] == "transcript" and m["final"]]
                assert len(finals) == 1 and finals[0]["turn_id"] == started["turn_id"] == msg["turn_id"]
                return {"first_downlink_ms": round(first, 2), "sample_rate": started["sample_rate"],
                        "audio_ms": round(duration, 2), "chunks": len(audio), "verified_groups": len(groups),
                        "metadata_before_pcm": True, "final_transcripts": [m["text"] for m in finals],
                        "final_transcript_turn_id": finals[0]["turn_id"]}, pcm

            start = time.perf_counter()
            ws.send_json({"type": "text", "text": "退款多久到账"})
            result["text_turn"], pcm = collect(start)
            from math import gcd
            import numpy as np
            from scipy.signal import resample_poly
            rate = result["text_turn"]["sample_rate"]
            divisor = gcd(rate, 16000)
            samples = resample_poly(np.frombuffer(pcm, dtype="<i2").astype(np.float32),
                                    16000 // divisor, rate // divisor).clip(-32768, 32767).astype("<i2")
            start = time.perf_counter()
            ws.send_json({"type": "speech_started"})
            for seq, offset in enumerate(range(0, len(samples), 320)):
                payload = samples[offset:offset + 320].tobytes().ljust(640, b"\0")
                ws.send_bytes(struct.pack(">BBHHIIH", 0xA1, 1, 16000, 320, seq, seq * 20, 0) + payload)
            ws.send_json({"type": "speech_ended"})
            result["asr_turn"], _ = collect(start)
            assert result["asr_turn"]["final_transcripts"][0].strip()
            assert result["asr_turn"]["final_transcript_turn_id"] != result["text_turn"]["final_transcript_turn_id"]
            ws.send_json({"type": "text", "text": "退款多久到账"})
            while True:
                message = ws.receive_json()
                assert message["type"] != "error", message
                if message["type"] == "audio_chunk" and message["pcm_b64"]:
                    break
            start = time.perf_counter()
            ws.send_json({"type": "speech_started"})
            cancelled = False
            while True:
                message = ws.receive_json()
                if cancelled:
                    assert message["type"] != "audio_chunk"
                if message["type"] == "cancel":
                    cancelled = True
                    result["cancel_ack_ms"] = round((time.perf_counter() - start) * 1000, 2)
                if message["type"] == "turn_aborted":
                    break
            assert cancelled
    if os.getenv("VOICE_TEST_METRICS_DIR"):
        root = Path(os.environ["VOICE_TEST_METRICS_DIR"]).resolve()
        root.relative_to(Path.cwd().resolve())
        root.mkdir(parents=True, exist_ok=True)
        (root / f"asgi-{provider}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_fake_asr_accepts_client_endpointing():
    """Contest mode passes client_endpointing to every ASR provider."""
    stream = await FakeAsr().start("s_test", client_endpointing=True)
    await stream.finish()
    events = [event async for event in stream.events()]
    assert events and events[0].kind == "final"
