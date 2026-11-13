"""In-process LiveKit bridge for an existing Orchestrator session.

The browser keeps its normal WS connection for control messages, transcripts,
state and turn results. LiveKit carries only media. This bridge is started from
`POST /sessions/{id}/rtc` so the room, Orchestrator session and frontend are the
same session, not a second bridge-created call.
"""
from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

from ..media.livekit import create_livekit_token
from ..voice.types import AudioFrame


class BridgeConnection:
    """Orchestrator Connection: JSON to browser WS, audio to LiveKit."""

    def __init__(self, bridge: "LiveKitBridge") -> None:
        self.bridge = bridge

    async def send_json(self, data: dict[str, Any]) -> None:
        target = self.bridge.browser_ws
        if target is not None:
            await target.send_json(data)

    async def send_audio(self, data: dict[str, Any], cancel: Any) -> None:
        cancel.check()
        raw = data.get("pcm_b64") or ""
        if not raw:
            return
        pcm = base64.b64decode(raw)
        if not pcm:
            return
        frame = self.bridge.audio_frame(pcm, int(data.get("sample_rate") or self.bridge.sample_rate))
        await self.bridge.source.capture_frame(frame)
        self.bridge.sent_audio += 1
        if self.bridge.sent_audio == 1:
            print("[livekit-bridge] first TTS audio frame published", flush=True)


class LiveKitBridge:
    def __init__(self, app: Any, session_id: str, room_name: str,
                 url: str, api_key: str, api_secret: str) -> None:
        from livekit import rtc

        self.rtc = rtc
        self.app = app
        self.session_id = session_id
        self.room_name = room_name
        self.url = url
        self.api_key = api_key
        self.api_secret = api_secret
        self.orc = app.state.orchestrator
        self.voice = app.state.voice
        self.browser_ws = self.orc.get_session(session_id).ws
        self.sample_rate = self.orc.tts.sample_rate
        self.source = rtc.AudioSource(self.sample_rate, 1)
        self.track = rtc.LocalAudioTrack.create_audio_track("agent-voice", self.source)
        self.room = rtc.Room()
        self.conn = BridgeConnection(self)
        self.asr = None
        self.asr_task: asyncio.Task | None = None
        self.transcript_task: asyncio.Task | None = None
        self.pending_text: str | None = None
        self.pending_event = asyncio.Event()
        self.user_joined = asyncio.Event()
        self.user_track_ready = asyncio.Event()
        self.opened = False
        self.sent_audio = 0
        self.stopped = False

    def audio_frame(self, pcm: bytes, rate: int):
        if rate != self.sample_rate:
            raise ValueError(f"TTS rate {rate} != source rate {self.sample_rate}")
        return self.rtc.AudioFrame(pcm, rate, 1, len(pcm) // 2)

    async def start(self) -> None:
        rtc = self.rtc
        self.asr = await self.voice.start_asr(self.session_id)
        self.asr_task = asyncio.create_task(self._consume_asr())
        self.transcript_task = asyncio.create_task(self._consume_transcripts())

        @self.room.on("track_subscribed")
        def on_track(track, publication, participant) -> None:
            if track.kind == rtc.TrackKind.KIND_AUDIO:
                self.user_track_ready.set()
                asyncio.create_task(self._consume_track(track, participant.identity))
                # Only speak the outbound greeting after the browser has
                # actually subscribed; otherwise early TTS is never heard.
                asyncio.create_task(self._open_once())

        @self.room.on("participant_connected")
        def on_participant(participant) -> None:
            self.user_joined.set()

        @self.room.on("disconnected")
        def on_disconnected(*_: Any) -> None:
            self.stopped = True

        token = create_livekit_token(
            self.api_key, self.api_secret, f"agent-{self.session_id[:10]}", self.room_name,
        )
        await self.room.connect(self.url, token)
        await self.room.local_participant.publish_track(
            self.track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
        )
        print(f"[livekit-bridge] room={self.room_name} session={self.session_id} connected", flush=True)

    async def _open_once(self) -> None:
        if self.opened or not getattr(self.orc.agent, "opens_outbound", False):
            return
        self.opened = True
        await asyncio.sleep(0.3)
        await self.orc.open_outbound(self.session_id, self.conn)

    async def _consume_asr(self) -> None:
        try:
            async for event in self.asr.events():
                if event.kind == "final" and event.text.strip():
                    print(f"[livekit-bridge] asr: {event.text}", flush=True)
                    # Keep only the newest pending transcript. If the user
                    # starts a new utterance while an older one is waiting,
                    # the older one is discarded instead of being replayed.
                    self.pending_text = event.text
                    self.pending_event.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[livekit-bridge] asr consumer failed: {type(exc).__name__}: {exc}", flush=True)

    async def _consume_transcripts(self) -> None:
        while True:
            await self.pending_event.wait()
            self.pending_event.clear()
            text, self.pending_text = self.pending_text, None
            if not text:
                continue
            try:
                await self.orc.handle_text(self.session_id, text, self.conn)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[livekit-bridge] transcript worker failed: {type(exc).__name__}: {exc}", flush=True)

    async def _consume_track(self, track: Any, identity: str) -> None:
        print(f"[livekit-bridge] subscribe {identity}", flush=True)
        stream = self.rtc.AudioStream(track, sample_rate=16000, num_channels=1)
        seq = 0
        try:
            async for event in stream:
                await self.asr.push(AudioFrame(event.frame.data.tobytes(), seq, 0))
                seq += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[livekit-bridge] audio consumer failed: {type(exc).__name__}: {exc}", flush=True)

    async def stop(self) -> None:
        self.stopped = True
        if self.asr_task:
            self.asr_task.cancel()
        if self.transcript_task:
            self.transcript_task.cancel()
        if self.asr:
            await self.asr.close()
        await self.room.disconnect()


class BridgeRegistry:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.bridges: dict[str, LiveKitBridge] = {}

    async def start(self, session_id: str, room_name: str) -> LiveKitBridge | None:
        import os
        url = os.getenv("LIVEKIT_URL", "").strip()
        key = os.getenv("LIVEKIT_API_KEY", "").strip()
        secret = os.getenv("LIVEKIT_API_SECRET", "").strip()
        if not (url and key and secret):
            return None
        if session_id in self.bridges:
            return self.bridges[session_id]
        bridge = LiveKitBridge(self.app, session_id, room_name, url, key, secret)
        self.bridges[session_id] = bridge
        await bridge.start()
        return bridge

    async def stop(self, session_id: str) -> None:
        bridge = self.bridges.pop(session_id, None)
        if bridge:
            await bridge.stop()

    async def stop_all(self) -> None:
        for session_id in list(self.bridges):
            await self.stop(session_id)
