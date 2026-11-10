"""LiveKit server-side media bridge (MVP).

Joins a LiveKit room as an agent, subscribes to the user's microphone track,
runs sherpa ASR, and publishes a Melo TTS response back into the room.

This is intentionally a media bridge, not the production Orchestrator:
- no turn/compliance/tool orchestration yet
- the reply is an ASR echo, used to verify the full audio path

Run from the repo root (the SDK is installed outside the repo):

  $env:PYTHONPATH='H:\\aicall\\.livekit311;H:\\aicall\\.web311;H:\\pytorch-env\\Lib\\site-packages'
  H:\\pytorch-env\\python.exe scripts\\livekit_bridge.py `
      --room aicall-<session> --identity agent `
      --asr-model H:\\aicall\\models\\asr --tts-model H:\\aicall\\models\\melo
"""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from typing import Any

from livekit import rtc

from app.cancel import CancelToken
from app.media.livekit import create_livekit_token
from app.voice.melo import MeloTts
from app.voice.sherpa_asr import SherpaAsr
from app.voice.types import AudioFrame


async def run(args: argparse.Namespace) -> None:
    asr = SherpaAsr(args.asr_model)
    stream = await asr.start("livekit-bridge")
    tts = MeloTts(args.tts_model)
    speak_queue: asyncio.Queue[str] = asyncio.Queue()
    stop = asyncio.Event()
    audio_source = rtc.AudioSource(48000, 1)
    audio_track = rtc.LocalAudioTrack.create_audio_track("agent-voice", audio_source)

    room = rtc.Room()

    async def consume_asr() -> None:
        async for event in stream.events():
            if event.kind == "final" and event.text.strip():
                print(f"[asr] {event.text}", flush=True)
                await speak_queue.put(event.text)

    async def speak_loop() -> None:
        while not stop.is_set():
            text = await speak_queue.get()
            print(f"[tts] {text}", flush=True)
            cancel = CancelToken()
            async for chunk in tts.synthesize(text, sample_rate=48000, cancel=cancel):
                if chunk.pcm:
                    frame = rtc.AudioFrame(chunk.pcm, 48000, 1, len(chunk.pcm) // 2)
                    await audio_source.capture_frame(frame)

    async def consume_track(track: rtc.Track, identity: str) -> None:
        print(f"[room] subscribing {identity} {track.kind}", flush=True)
        audio_stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
        seq = 0
        try:
            async for event in audio_stream:
                frame = event.frame
                if seq == 0 or seq % 50 == 0:
                    print(f"[audio] {identity} seq={seq} rate={frame.sample_rate} "
                          f"ch={frame.num_channels} samples={frame.samples_per_channel} "
                          f"bytes={len(frame.data)}", flush=True)
                await stream.push(AudioFrame(frame.data.tobytes(), seq, 0))
                seq += 1
        except Exception as exc:
            print(f"[audio] consumer failed: {type(exc).__name__}: {exc}", flush=True)
            import traceback
            traceback.print_exc()

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
        audio_track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    )
    print("[room] connected; waiting for microphone", flush=True)

    asr_task = asyncio.create_task(consume_asr())
    tts_task = asyncio.create_task(speak_loop())
    try:
        await stop.wait()
    finally:
        asr_task.cancel()
        tts_task.cancel()
        await stream.close()
        await room.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.getenv("LIVEKIT_URL", "ws://127.0.0.1:7880"))
    parser.add_argument("--api-key", default=os.getenv("LIVEKIT_API_KEY", "devkey"))
    parser.add_argument("--api-secret", default=os.getenv("LIVEKIT_API_SECRET", "secret"))
    parser.add_argument("--room", required=True)
    parser.add_argument("--identity", default="agent")
    parser.add_argument("--asr-model", type=Path, required=True)
    parser.add_argument("--tts-model", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
