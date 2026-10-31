#!/usr/bin/env python3
"""Headless WebSocket E2E: 2 turns + barge-in + hangup.

Usage:
  python scripts/e2e_ws.py --base http://127.0.0.1:8081
"""

from __future__ import annotations

import argparse
import asyncio
import json
import urllib.request

import websockets


def post(base: str, path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read().decode("utf-8"))


async def recv_until(ws, wanted: set[str], *, timeout: float = 8.0,
                     on_message=None) -> list[str]:
    seen: list[str] = []
    while True:
        message = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        seen.append(message["type"])
        if on_message:
            await on_message(message)
        if message["type"] in wanted:
            return seen


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8081")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    session = post(base, "/sessions", {"mode": "voice"})
    assert "?token=" in session["ws_url"], "ws_url must carry token"
    ws_url = "ws://" + base.split("://", 1)[1] + session["ws_url"]
    print("session", session["session_id"])

    async with websockets.connect(ws_url) as ws:
        ready = json.loads(await ws.recv())
        assert ready["type"] == "session_ready"
        await ws.recv()  # state
        print("ready", ready.get("protocol_version"))

        # Turn 1: normal order flow.
        await ws.send(json.dumps({"type": "text", "text": "我想改一下订单"}))

        first_done = False

        async def playback(message: dict) -> None:
            nonlocal first_done
            if message["type"] == "agent_text" and not first_done:
                await ws.send(json.dumps({
                    "type": "playback_first_frame",
                    "turn_id": message["turn_id"],
                    "reply_id": message["reply_id"],
                    "client_mono_ms": 1.0,
                    "audio_ctx_time": 0.1,
                }))
                first_done = True

        seen1 = await recv_until(ws, {"turn_result"}, on_message=playback)
        for required in ("tts_started", "tts_sentence", "tts_completed", "turn_result"):
            assert required in seen1, (required, seen1)
        print("turn1", "ok")

        # Turn 2: barge-in after the first sentence.
        await ws.send(json.dumps({"type": "text", "text": "再帮我改一下订单"}))
        seen2 = await recv_until(ws, {"agent_text"}, timeout=5.0)
        await ws.send(json.dumps({
            "type": "speech_started", "client_ts_ms": 1, "client_mono_ms": 1.0,
            "source": "local_vad",
        }))
        await ws.send(json.dumps({
            "type": "local_muted", "turn_id": None, "client_mono_ms": 2.0,
            "reason": "barge_in", "played_ms": 100, "sentence_id": 1,
            "char_idx": 2, "last_frame_played_mono_ms": 2.0, "stop_latency_ms": 20.0,
        }))
        seen3 = await recv_until(ws, {"turn_aborted"}, timeout=5.0)
        print("barge-in", "ok" if "turn_aborted" in seen3 else "missing")

        await ws.send(json.dumps({"type": "hangup", "reason": "user_click"}))
        ended = None
        for _ in range(8):
            message = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            if message["type"] == "session_ended":
                ended = message
                break
        assert ended is not None, "session_ended not received"
        print("hangup", ended["type"])
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
