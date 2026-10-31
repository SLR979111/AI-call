#!/usr/bin/env python3
"""Capture a real WebSocket barge-in session as a golden JSONL fixture.

The tool drives the documented voice session end to end:

  1. ``POST /sessions {"mode": "voice"}`` to obtain ``session_id``/``token``/``ws_url``
  2. connect to ``ws_url`` and record ``session_ready`` + initial ``state``
  3. send ``hello``, then a first ``text`` turn (order change) while acking the
     first ``agent_text`` of every reply with ``playback_first_frame``
  4. start a second ``text`` turn, then barge in on its first ``agent_text``
     with ``speech_started`` + ``local_muted``
  5. wait for ``turn_aborted``, send ``hangup``, wait for ``session_ended``

Every WebSocket frame sent or received is recorded verbatim, in order, as one
UTF-8 JSONL line::

  {"dir": "c2s", "frame": {...}}
  {"dir": "s2c", "frame": {...}}

Binary audio frames are rejected (there should be none; WS debug captures are
text-only in this protocol version).

Usage::

  PYTHONPATH=.deps python scripts/capture_ws_fixture.py \
      --base http://127.0.0.1:8095 \
      --out contracts/fixtures/ws_session_bargein.jsonl

Only ``websockets`` is required beyond the standard library.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Awaitable, Callable

import websockets

HELLO: dict[str, Any] = {
    "type": "hello",
    "client_version": "capture@0.1",
    "capabilities": ["local_vad", "aec"],
}

FIRST_TURN_TEXT = "我想改一下订单"
SECOND_TURN_TEXT = "再帮我改一下订单"

SPEECH_STARTED: dict[str, Any] = {
    "type": "speech_started",
    "client_ts_ms": 1,
    "client_mono_ms": 1.0,
    "source": "local_vad",
}

LOCAL_MUTED: dict[str, Any] = {
    "type": "local_muted",
    "turn_id": None,
    "client_mono_ms": 2.0,
    "reason": "barge_in",
    "played_ms": 100,
    "sentence_id": 1,
    "char_idx": 2,
    "last_frame_played_mono_ms": 2.0,
    "stop_latency_ms": 20.0,
}

HANGUP: dict[str, Any] = {"type": "hangup", "reason": "user_click"}

# Message types the resulting fixture is expected to cover. Capturing a
# barge-in session without these means the run was not representative.
REQUIRED_TYPES = (
    "session_ready",
    "state",
    "tts_started",
    "tts_sentence",
    "agent_text",
    "compliance",
    "turn_aborted",
    "turn_result",
    "session_ended",
)


class CaptureError(RuntimeError):
    """A protocol/transport condition that makes the capture unusable."""


def post_json(base: str, path: str, body: dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        base + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise CaptureError(f"POST {path} failed: {exc.code} {exc.reason}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise CaptureError(f"POST {path} failed: cannot reach {base}: {exc.reason}") from exc


def get_json(base: str, path: str, timeout: float = 5.0) -> dict[str, Any]:
    request = urllib.request.Request(base + path, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise CaptureError(f"GET {path} failed: {exc.code} {exc.reason}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise CaptureError(f"GET {path} failed: cannot reach {base}: {exc.reason}") from exc


class Recorder:
    """Ordered, verbatim log of both directions of one WebSocket session."""

    def __init__(self) -> None:
        self.lines: list[dict[str, Any]] = []

    async def send(self, ws: Any, frame: dict[str, Any]) -> None:
        await ws.send(json.dumps(frame, ensure_ascii=False))
        self.lines.append({"dir": "c2s", "frame": frame})

    async def recv(self, ws: Any, timeout: float) -> dict[str, Any]:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise CaptureError(f"timed out after {timeout:.1f}s waiting for a WebSocket frame") from exc
        if isinstance(raw, (bytes, bytearray)):
            raise CaptureError("server sent a binary frame; this fixture records text frames only")
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CaptureError(f"server sent non-JSON text frame: {raw[:200]!r}") from exc
        if not isinstance(frame, dict):
            raise CaptureError(f"server frame is not a JSON object: {frame!r}")
        self.lines.append({"dir": "s2c", "frame": frame})
        return frame

    def types(self) -> list[str]:
        return [line["frame"].get("type", "") for line in self.lines]

    def write(self, out: Path) -> None:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8", newline="\n") as handle:
            for line in self.lines:
                handle.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")


class PlaybackAck:
    """Send ``playback_first_frame`` once for the first ``agent_text`` of a reply."""

    def __init__(self, recorder: Recorder, ws: Any) -> None:
        self.recorder = recorder
        self.ws = ws
        self.acked_turns: set[str] = set()

    async def on_frame(self, frame: dict[str, Any]) -> None:
        if frame.get("type") != "agent_text":
            return
        turn_id = frame.get("turn_id")
        if not turn_id or turn_id in self.acked_turns:
            return
        self.acked_turns.add(turn_id)
        await self.recorder.send(self.ws, {
            "type": "playback_first_frame",
            "turn_id": turn_id,
            "reply_id": frame.get("reply_id"),
            "client_mono_ms": 1.0,
            "audio_ctx_time": 0.1,
        })


async def recv_until(
    recorder: Recorder,
    ws: Any,
    wanted: set[str],
    *,
    timeout: float,
    on_frame: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Receive frames until one of ``wanted`` arrives; every frame is recorded."""
    while True:
        frame = await recorder.recv(ws, timeout)
        if on_frame is not None:
            await on_frame(frame)
        if frame.get("type") in wanted:
            return frame


async def wait_until_idle(base: str, session_id: str, timeout: float = 3.0) -> None:
    """Wait for the server to clear ``current_turn`` before opening the next turn.

    Without this the next ``text`` can race the previous turn's teardown, making
    ``handle_text`` cancel a turn that just finished and injecting a stray
    ``cancel`` frame into the golden transcript.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            state = get_json(base, f"/sessions/{session_id}/state", timeout=2.0)
        except CaptureError:
            state = {}
        if state.get("turn_id") is None:
            return
        await asyncio.sleep(0.02)
    print("warning: session did not report an idle turn before the next turn", file=sys.stderr)


async def capture(args: argparse.Namespace) -> tuple[Recorder, str]:
    base = args.base.rstrip("/")
    session = post_json(base, "/sessions", {"mode": "voice"})
    session_id = session.get("session_id")
    ws_url = session.get("ws_url")
    if not session_id or not ws_url:
        raise CaptureError(f"POST /sessions returned no session_id/ws_url: {session}")
    if "?token=" not in ws_url:
        raise CaptureError(f"ws_url does not carry a token: {ws_url}")
    if base.startswith("https://"):
        ws_base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        ws_base = "ws://" + base[len("http://"):]
    else:
        raise CaptureError(f"--base must start with http:// or https://, got {base!r}")
    url = ws_base + ws_url
    print(f"session {session_id}")
    print(f"ws {url}")

    recorder = Recorder()
    async with websockets.connect(url, max_size=2 ** 22, open_timeout=args.timeout) as ws:
        ready = await recorder.recv(ws, args.timeout)
        if ready.get("type") != "session_ready":
            raise CaptureError(f"expected session_ready first, got {ready.get('type')!r}")
        initial_state = await recorder.recv(ws, args.timeout)
        if initial_state.get("type") != "state":
            raise CaptureError(f"expected initial state, got {initial_state.get('type')!r}")

        await recorder.send(ws, HELLO)
        ack = await recv_until(recorder, ws, {"hello_ack", "error"}, timeout=args.timeout)
        if ack.get("type") == "error":
            raise CaptureError(f"hello rejected: {ack.get('error')}")

        # ---- turn 1: normal order-change turn, acked playback ----
        playback = PlaybackAck(recorder, ws)
        await recorder.send(ws, {"type": "text", "text": FIRST_TURN_TEXT})
        first = await recv_until(
            recorder, ws, {"turn_result", "turn_aborted"},
            timeout=args.timeout, on_frame=playback.on_frame,
        )
        if first.get("type") != "turn_result":
            raise CaptureError(f"turn 1 ended unexpectedly: {first}")
        print(f"turn 1 {first.get('turn_id')} ok")
        await wait_until_idle(base, session_id)

        # ---- turn 2: barge in on the first agent_text ----
        barge_state = {"sent": False}

        async def barge(frame: dict[str, Any]) -> None:
            await playback.on_frame(frame)
            if frame.get("type") != "agent_text" or barge_state["sent"]:
                return
            barge_state["sent"] = True
            await recorder.send(ws, SPEECH_STARTED)
            await recorder.send(ws, LOCAL_MUTED)

        await recorder.send(ws, {"type": "text", "text": SECOND_TURN_TEXT})
        second = await recv_until(
            recorder, ws, {"turn_aborted", "turn_result"},
            timeout=args.timeout, on_frame=barge,
        )
        if second.get("type") != "turn_aborted":
            raise CaptureError(f"barge-in did not abort turn 2: {second}")
        print(f"turn 2 {second.get('turn_id')} aborted ({second.get('reason')})")

        # ---- hangup ----
        await recorder.send(ws, HANGUP)
        ended = await recv_until(recorder, ws, {"session_ended"}, timeout=args.timeout)
        print(f"session ended: {ended.get('reason')} duration_ms={ended.get('duration_ms')}")
    return recorder, session_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture a real WS barge-in session as JSONL.")
    parser.add_argument("--base", default="http://127.0.0.1:8095", help="HTTP base of the FastAPI app")
    parser.add_argument(
        "--out", default="contracts/fixtures/ws_session_bargein.jsonl",
        help="output JSONL path",
    )
    parser.add_argument("--timeout", type=float, default=15.0, help="per-frame receive timeout (s)")
    args = parser.parse_args(argv)

    try:
        recorder, session_id = asyncio.run(capture(args))
    except CaptureError as exc:
        print(f"capture failed: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"capture failed: cannot connect: {exc}", file=sys.stderr)
        return 1

    out = Path(args.out)
    recorder.write(out)

    observed = recorder.types()
    missing = [name for name in REQUIRED_TYPES if name not in observed]
    print(f"wrote {out} ({len(recorder.lines)} frames)")
    print("types: " + ", ".join(observed))
    if missing:
        print(f"capture failed: fixture is missing required types: {', '.join(missing)}",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
