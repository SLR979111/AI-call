"""LiveKit server-side bridge into the real Orchestrator/Agent stack.

Joins a LiveKit room as an agent, subscribes to the user's microphone track,
feeds 16 kHz PCM into the same sherpa ASR used by `/rtc`, and routes final
transcripts through the existing Orchestrator (Agent + Tool + Compliance + TTS).
TTS PCM is published back into the room.

Run (SDK lives outside the repo):
  $env:PYTHONPATH='H:\\aicall;H:\\aicall\\.livekit311;H:\\aicall\\.web311;H:\\pytorch-env\\Lib\\site-packages'
  H:\\pytorch-env\\python.exe scripts\\livekit_bridge.py `
      --room aicall-<session> --identity agent `
      --asr-model H:\\aicall\\models\\asr --tts-model H:\\aicall\\models\\melo `
      --context '{"request_id":"Q983001","brand":"演示松潮生活","reject_l1":"入驻","reject_l2":"公司主体","reject_l3":"主体资质到期"}'
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
from typing import Any

from livekit import rtc

from app.media.livekit import create_livekit_token


class LiveKitConnection:
    """Orchestrator Connection + audio sink for one LiveKit agent participant."""

    def __init__(self, room: rtc.Room, source: rtc.AudioSource, sample_rate: int) -> None:
        self.room = room
        self.source = source
        self.sample_rate = sample_rate
        self.sent_audio = 0

    async def send_json(self, data: dict[str, Any]) -> None:
        # The current test page does not parse a data-channel protocol yet; keep
        # the same messages in the bridge log for observability.
        kind = data.get("type")
        if kind in {"transcript", "agent_text", "state", "turn_result", "error"}:
            detail = data.get("text") or data.get("turn_state") or data.get("result") or ""
            print(f"[event] {kind} {str(detail)[:160]}", flush=True)

    async def send_audio(self, data: dict[str, Any], cancel: Any) -> None:
        cancel.check()
        raw = data.get("pcm_b64") or ""
        if not raw:
            return
        rate = int(data.get("sample_rate") or self.sample_rate)
        if rate != self.sample_rate:
            # Orchestrator and source are both configured to the TTS rate.
            return
        pcm = base64.b64decode(raw)
        if not pcm:
            return
        frame = rtc.AudioFrame(pcm, rate, 1, len(pcm) // 2)
        await self.source.capture_frame(frame)
        self.sent_audio += 1
        if self.sent_audio == 1:
            print("[tts] first audio frame published to LiveKit", flush=True)


async def run(args: argparse.Namespace) -> None:
    # app.main reads settings at import time; set the profile before importing it.
    context = json.loads(args.context)
    os.environ.update({
        "APP_ENV": "dev",
        "DEV_NO_AUTH": "1",
        "SCENARIO_ID": "pop_qualification_audit",
        "ASR_PROVIDER": "sherpa",
        "TTS_PROVIDER": "melo",
        "TTS_FALLBACK_PROVIDER": "",
        "ASR_MODEL_DIR": str(args.asr_model),
        "MELO_MODEL_DIR": str(args.tts_model),
        "POP_CONTEST": "1",
        "POP_CONTEST_POLICY": "guided",
        "POP_CONTEST_CONTEXT": json.dumps(context, ensure_ascii=False),
        "DATA_DIR": str(args.data_dir),
    })
    from app.main import app, lifespan

    stop = asyncio.Event()
    async with lifespan(app):
        orc = app.state.orchestrator
        voice = app.state.voice
        session = await orc.create_session("voice", case_id=args.case_id)

        source = rtc.AudioSource(orc.tts.sample_rate, 1)
        track = rtc.LocalAudioTrack.create_audio_track("agent-voice", source)
        conn = LiveKitConnection(room=None, source=source, sample_rate=orc.tts.sample_rate)
        room = rtc.Room()
        conn.room = room

        asr = await voice.start_asr(session.session_id)

        async def consume_asr() -> None:
            async for event in asr.events():
                if event.kind == "final" and event.text.strip():
                    print(f"[asr] {event.text}", flush=True)
                    await orc.handle_text(session.session_id, event.text, conn)

        async def consume_track(track: rtc.Track, identity: str) -> None:
            print(f"[room] subscribing {identity} {track.kind}", flush=True)
            stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
            seq = 0
            try:
                async for event in stream:
                    frame = event.frame
                    await asr.push(_audio_frame(frame.data.tobytes(), seq))
                    seq += 1
            except Exception as exc:
                print(f"[audio] consumer failed: {type(exc).__name__}: {exc}", flush=True)

        @room.on("track_subscribed")
        def on_track(track: rtc.Track, publication: rtc.TrackPublication,
                     participant: rtc.RemoteParticipant) -> None:
            if track.kind == rtc.TrackKind.KIND_AUDIO:
                asyncio.create_task(consume_track(track, participant.identity))

        @room.on("disconnected")
        def on_disconnected(*_: Any) -> None:
            print("[room] disconnected", flush=True)
            stop.set()

        token = create_livekit_token(args.api_key, args.api_secret, args.identity, args.room)
        print(f"[room] connecting {args.url} room={args.room} identity={args.identity}", flush=True)
        await room.connect(args.url, token)
        await room.local_participant.publish_track(
            track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )
        await orc.attach(session.session_id, conn)
        print(f"[room] connected; session={session.session_id}", flush=True)

        asr_task = asyncio.create_task(consume_asr())
        # Outbound POP agent speaks first once the media track is published.
        if getattr(orc.agent, "opens_outbound", False):
            await orc.open_outbound(session.session_id, conn)

        try:
            await stop.wait()
        finally:
            asr_task.cancel()
            await asr.close()
            await room.disconnect()


def _audio_frame(pcm: bytes, seq: int):
    from app.voice.types import AudioFrame
    return AudioFrame(pcm, seq, 0)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.getenv("LIVEKIT_URL", "ws://127.0.0.1:7880"))
    parser.add_argument("--api-key", default=os.getenv("LIVEKIT_API_KEY", "devkey"))
    parser.add_argument("--api-secret", default=os.getenv("LIVEKIT_API_SECRET", "secret"))
    parser.add_argument("--room", required=True)
    parser.add_argument("--identity", default="agent")
    parser.add_argument("--case-id", default="pop-scene-034")
    parser.add_argument("--asr-model", type=Path, required=True)
    parser.add_argument("--tts-model", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, default=Path("data/livekit-bridge"))
    parser.add_argument(
        "--context",
        default='{"request_id":"Q983001","brand":"演示松潮生活",'
                '"reject_l1":"入驻","reject_l2":"公司主体","reject_l3":"主体资质到期"}',
    )
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
