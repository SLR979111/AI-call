"""Compare two local Melo speeds with paired, identical text and saved PCM."""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import os
import statistics
import sys
import time
import wave
from contextlib import nullcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.pop_competition import OPENING, RECORDING
from app.cancel import CancelToken
from app.eval.pop_runner import write_json
from app.voice.melo import MeloTts

TEXTS = {
    "opening": (OPENING, RECORDING),
    "reason": ("这次退回是因为主体资质到期。", "下一步，请更新有效资质后重新提交。"),
    "channel": ("请打开京麦，在原申请里查看驳回意见，按页面提示补交。",),
}


async def run(args):
    args.output.mkdir(parents=True, exist_ok=False)
    model = MeloTts(str(args.model))
    versions = {}
    for speed in (1.0, args.speed):
        model.speed = speed  # Reuse identical weights on the same CPU for the paired test.
        timings, rows = [], {}
        for label, sentences in TEXTS.items():
            pcm = bytearray()
            for sentence in sentences:
                started = time.perf_counter()
                first = None
                async for chunk in model.synthesize(sentence, sample_rate=model.sample_rate, cancel=CancelToken()):
                    if chunk.pcm:
                        if first is None:
                            first = (time.perf_counter() - started) * 1000
                        pcm.extend(chunk.pcm)
                if first is None:
                    raise ValueError("empty TTS clip")
                timings.append(first)
            file = f"{label}-{speed:.2f}.wav"
            with wave.open(str(args.output / file), "wb") as stream:
                stream.setparams((1, 2, model.sample_rate, 0, "NONE", "not compressed"))
                stream.writeframes(pcm)
            rows[label] = {"file": file, "duration_ms": round(len(pcm) / 2 / model.sample_rate * 1000, 2)}
        versions[f"{speed:.2f}"] = {"clips": rows, "n": len(timings),
                                    "first_pcm_p50_ms": round(statistics.median(timings), 2),
                                    "first_pcm_ms": [round(item, 2) for item in timings]}
    write_json(args.output / "comparison.json", {"text": TEXTS, "model": args.model.name,
               "sample_rate": model.sample_rate, "measure": "first CPU PCM, not acoustic or naturalness",
               "profiles": versions})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--dll-dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--speed", type=float, default=.93)
    args = parser.parse_args()
    if not 0.75 <= args.speed <= 1.25 or args.speed == 1.0:
        parser.error("choose an alternative speed between 0.75 and 1.25")
    with os.add_dll_directory(str(args.dll_dir.resolve())) if args.dll_dir else nullcontext():
        if args.dll_dir:
            ctypes.WinDLL(str(args.dll_dir.resolve() / "onnxruntime.dll"))
        asyncio.run(run(args))
