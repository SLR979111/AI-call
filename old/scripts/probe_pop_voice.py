"""Replay synthetic Chinese WAV through the actual ASR, Agent and CPU TTS."""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import os
import re
import statistics
import time
import wave
from pathlib import Path

import numpy as np

from eval_pop_followups import PREVIOUS_RUN, compile_cases, policies
from app.cancel import CancelToken
from app.compliance import ComplianceGuard
from app.eval.pop_rules import score_case
from app.eval.pop_runner import KNOWLEDGE, CaseAgent, write_json
from app.events import EventBus
from app.orchestrator import Orchestrator
from app.rsi.pop_strategy import build_agent
from app.tools.gateway import MockBusiness, ToolGateway
from app.voice.melo import MeloTts
from app.voice.sherpa_asr import SherpaAsr
from app.voice.types import AudioFrame

RATE = 16000
FRAME_BYTES = 640
SPOKEN_CONFIRMATION_FILLER = re.compile(r"^(?:是的|对的|没错|嗯|这个|啊)[，,。 ]*(?=我(?:就)?是)")


def write_wav(path: Path, pcm: bytes, rate: int) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(pcm)


def with_noise(pcm: bytes, snr: float, seed: int) -> bytes:
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    rms = np.sqrt(np.mean(samples**2))
    noise = np.random.default_rng(seed).normal(0, rms / 10 ** (snr / 20), size=samples.shape)
    return np.clip(samples + noise, -32768, 32767).astype("<i2").tobytes()


async def transcribe(asr, pcm: bytes, session_id: str):
    stream = await asr.start(session_id)
    events_task = asyncio.create_task(collect(stream))
    for seq, offset in enumerate(range(0, len(pcm), FRAME_BYTES)):
        chunk = pcm[offset:offset + FRAME_BYTES]
        await stream.push(AudioFrame(chunk.ljust(FRAME_BYTES, b"\x00"), seq, seq * 20))
    finish_started = time.perf_counter()
    await stream.finish()
    events = await events_task
    return (
        "，".join(event.text for event in events if event.kind == "final" and event.text),
        (time.perf_counter() - finish_started) * 1000,
        [event.error for event in events if event.kind == "error"],
    )


async def collect(stream):
    return [event async for event in stream.events()]


class RecordingTts:
    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.sample_rate = delegate.sample_rate
        self.reset()

    def reset(self):
        self.pcm = bytearray()
        self.first_chunk = None

    async def synthesize(self, text, **options):
        async for chunk in self.delegate.synthesize(text, **options):
            if chunk.pcm:
                if self.first_chunk is None:
                    self.first_chunk = time.perf_counter()
                self.pcm.extend(chunk.pcm)
            yield chunk


async def replay(case, policy, folder, mode, inputs, asr, tts, *, reuse=None, format_confirmation=False):
    folder.mkdir(parents=True, exist_ok=False)
    release = policy["release_id"]
    bus = EventBus(folder, release, "pop-audio-probe", mode="voice")
    mock = MockBusiness()
    mock.fault = "500" if case.get("tool_fault") == "failed" else None
    tools = ToolGateway(folder, mock, bus)
    recorder = RecordingTts(tts)
    agent = CaseAgent(build_agent(policy, KNOWLEDGE), case["context"])
    orchestrator = Orchestrator(bus, agent, tools, ComplianceGuard(), release,
                                "pop_qualification_audit", tts=recorder, tts_provider="melo")
    traces = []
    score_turns = []
    try:
        session = await orchestrator.create_session("voice")
        for index, (step, pcm) in enumerate(zip(case["steps"], inputs, strict=True), start=1):
            expected = step["say"]
            asr_ms = None
            errors = []
            if mode == "reference":
                recognized = expected
            else:
                if reuse:
                    with wave.open(str(reuse / f"user-{index}.wav"), "rb") as saved:
                        if (saved.getframerate(), saved.getnchannels(), saved.getsampwidth()) != (RATE, 1, 2):
                            raise ValueError("saved input must be 16 kHz mono PCM16")
                        audio = saved.readframes(saved.getnframes())
                else:
                    audio = with_noise(pcm, 15, index) if mode == "noise15" else pcm
                write_wav(folder / f"user-{index}.wav", audio, RATE)
                recognized, asr_ms, errors = await transcribe(asr, audio, session.session_id)
            delivered = recognized
            if mode != "reference" and format_confirmation:
                delivered = SPOKEN_CONFIRMATION_FILLER.sub("", recognized)
            row = {"reference": expected, "recognized": recognized, "asr_finish_ms": asr_ms,
                   "delivered_text": delivered, "asr_errors": errors, "sentences": [], "outcome": None}
            traces.append(row)
            if not recognized or errors:
                continue
            recorder.reset()
            started = time.perf_counter()
            result = await orchestrator.handle_text(session.session_id, delivered)
            inner = result.get("result") or {}
            row["sentences"] = result.get("sentences", [])
            row["outcome"] = inner.get("outcome")
            row["first_server_pcm_ms"] = round((recorder.first_chunk - started) * 1000, 2) if recorder.first_chunk else None
            if recorder.pcm:
                write_wav(folder / f"agent-{index}.wav", recorder.pcm, recorder.sample_rate)
            score_turns.append({"turn_id": result["turn_id"], "user_text": delivered,
                                "verified_identity": step.get("verifies_identity", False),
                                "sentences": row["sentences"], "result": inner})
        await orchestrator.end_session(session.session_id, "synthetic_audio_replay")
        events = bus.read_events(session.session_id)
    finally:
        tools.close()
        bus.close()
    score = score_case(case, {"turns": score_turns, "events": events, "tickets": list(mock.tickets.values())})
    output = {"case_id": case["case_id"], "mode": mode, "release_id": release, "turns": traces,
              "score": score, "events": bus.events_path(session.session_id).relative_to(folder).as_posix()}
    write_json(folder / "voice_trace.json", output)
    print(f"{case['case_id']} {mode}: {score['status']} ({', '.join(score['failure_labels']) or 'all checks pass'})",
          flush=True)
    return output


async def run(args):
    args.output.mkdir(parents=True, exist_ok=False)
    asr = SherpaAsr(str(args.asr_model), finish_padding_ms=args.asr_padding_ms)
    tts = MeloTts(str(args.tts_model))
    previous, candidate = policies(PREVIOUS_RUN)
    policy = previous if args.policy == "previous" else candidate
    cases = {case["case_id"]: case for case in compile_cases()}
    rows = []
    for case_id in args.cases:
        case = cases[case_id]
        inputs = []
        for step in case["steps"]:
            pcm = b""
            if not args.reuse_from and any(mode != "reference" for mode in args.modes):
                pcm = b"".join([chunk.pcm async for chunk in tts.synthesize(
                    step["say"], sample_rate=RATE, cancel=CancelToken(),
                )])
            inputs.append(pcm)
        for mode in args.modes:
            reuse = args.reuse_from / case_id / mode if args.reuse_from else None
            rows.append(await replay(case, policy, args.output / case_id / mode, mode, inputs, asr, tts,
                                     reuse=reuse, format_confirmation=args.format_confirmation))
    summary = {}
    for mode in args.modes:
        mode_rows = [row for row in rows if row["mode"] == mode]
        times = [turn["first_server_pcm_ms"] for row in mode_rows for turn in row["turns"]
                 if turn.get("first_server_pcm_ms") is not None]
        finals = [turn["asr_finish_ms"] for row in mode_rows for turn in row["turns"]
                  if turn.get("asr_finish_ms") is not None]
        summary[mode] = {
            "passed": sum(row["score"]["task_success"] for row in mode_rows), "cases": len(mode_rows),
            "first_server_pcm_ms": {"n": len(times), "p50": round(statistics.median(times), 2) if times else None},
            "asr_finish_ms": {"n": len(finals), "p50": round(statistics.median(finals), 2) if finals else None},
        }
    write_json(args.output / "summary.json", {
        "scope": "synthetic Mandarin WAV, accelerated 20 ms PCM frames, in-process first TTS PCM; no mic or playback",
        "model_paths": {"asr": args.asr_model.name, "tts": args.tts_model.name},
        "release_id": policy["release_id"],
        "asr_padding_ms": args.asr_padding_ms,
        "format_confirmation": args.format_confirmation,
        "input_reused": bool(args.reuse_from),
        "summary": summary,
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asr-model", required=True, type=Path)
    parser.add_argument("--tts-model", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--dll-dir", type=Path, help="Windows sherpa-onnx-core DLL folder")
    parser.add_argument("--reuse-from", type=Path, help="reuse byte-identical WAVs from a previous probe")
    parser.add_argument("--asr-padding-ms", type=int, choices=(300, 500), default=300)
    parser.add_argument("--format-confirmation", action="store_true", help="remove leading spoken fillers before explicit I-am statements")
    parser.add_argument("--policy", choices=("previous", "candidate"), default="candidate")
    parser.add_argument("--cases", nargs="+", default=["renewed-shop", "callback-channel", "past-employee"])
    parser.add_argument("--modes", nargs="+", choices=("reference", "clean", "noise15"),
                        default=["reference", "clean", "noise15"])
    args = parser.parse_args()
    if args.dll_dir:
        with os.add_dll_directory(str(args.dll_dir.resolve())):
            # Preload the matching DLL before Windows finds an older system-wide runtime.
            ctypes.WinDLL(str(args.dll_dir.resolve() / "onnxruntime.dll"))
            asyncio.run(run(args))
    else:
        asyncio.run(run(args))
