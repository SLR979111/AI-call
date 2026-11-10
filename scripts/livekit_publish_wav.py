"""Publish a 16 kHz WAV into a LiveKit room for bridge testing.

This is a test helper, not a product path. It joins as `test-user`, publishes
the WAV in 20 ms frames, then keeps the room alive briefly.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import wave
from pathlib import Path

from livekit import rtc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.media.livekit import create_livekit_token


async def run(args: argparse.Namespace) -> None:
    with wave.open(args.wav, "rb") as wav:
        assert wav.getnchannels() == 1 and wav.getframerate() == 16000, "need 16k mono wav"
        pcm = wav.readframes(wav.getnframes())
    source = rtc.AudioSource(16000, 1)
    track = rtc.LocalAudioTrack.create_audio_track("test-user-mic", source)
    room = rtc.Room()
    token = create_livekit_token(args.api_key, args.api_secret, "test-user", args.room)
    await room.connect(args.url, token)
    await room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
    samples = 320
    frame_bytes = samples * 2
    for offset in range(0, len(pcm), frame_bytes):
        chunk = pcm[offset:offset + frame_bytes]
        if len(chunk) < frame_bytes:
            chunk += b"\x00" * (frame_bytes - len(chunk))
        frame = rtc.AudioFrame(chunk, 16000, 1, samples)
        await source.capture_frame(frame)
        await asyncio.sleep(0.02)
    print("published", len(pcm) // 2, "samples", flush=True)
    # Keep publishing silence so ASR endpoint detection can finish the utterance.
    silence = b"\x00\x00" * 320
    for _ in range(100):
        frame = rtc.AudioFrame(silence, 16000, 1, 320)
        await source.capture_frame(frame)
        await asyncio.sleep(0.02)
    await asyncio.sleep(args.hold)
    await room.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:7880")
    parser.add_argument("--api-key", default="devkey")
    parser.add_argument("--api-secret", default="secret")
    parser.add_argument("--room", required=True)
    parser.add_argument("--wav", required=True)
    parser.add_argument("--hold", type=float, default=3.0)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
