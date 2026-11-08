"""Build matched synthetic user WAVs for the browser's endpoint diagnostic."""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import os
import shutil
from pathlib import Path

import numpy as np

from probe_pop_voice import RATE, write_wav
from app.cancel import CancelToken
from app.eval.pop_runner import digest, write_json
from app.voice.melo import MeloTts

PARTS = ("你们说的那条留言", "我得钻进后台哪一层啊")
GAPS_MS = (160, 520, 850)


async def build(args):
    args.output.mkdir(parents=True, exist_ok=False)
    for filename in ("turn-2.wav", "turn-3.wav"):
        shutil.copyfile(args.base / filename, args.output / filename)
    tts = MeloTts(str(args.tts_model))
    segments = []
    for text in PARTS:
        pcm = b"".join([chunk.pcm async for chunk in tts.synthesize(text, sample_rate=RATE, cancel=CancelToken())])
        # Trim synthesized lead/tail silence so the inserted gap is controlled.
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
        blocks = samples[:len(samples) // 320 * 320].reshape(-1, 320)
        active = np.flatnonzero(np.sqrt(np.mean(blocks ** 2, axis=1)) > 110)
        if not active.size:
            raise ValueError(f"No audible speech in {text}")
        segments.append(pcm[active[0] * 640:(active[-1] + 1) * 640])
    samples = []
    for gap_ms in GAPS_MS:
        name = f"pause-{gap_ms}.wav"
        silence = bytes(gap_ms * (RATE // 1000) * 2)
        write_wav(args.output / name, segments[0] + silence + segments[1] + bytes(RATE), RATE)
        samples.append({"file": name, "gap_ms": gap_ms, "sha256": digest(args.output / name)})
    write_json(args.output / "manifest.json", {"source": "synthetic Melo Mandarin", "reference": "，".join(PARTS),
               "tail_ms": 500, "samples": samples})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tts-model", type=Path, required=True)
    parser.add_argument("--dll-dir", type=Path, required=True)
    args = parser.parse_args()
    with os.add_dll_directory(str(args.dll_dir.resolve())):
        ctypes.WinDLL(str(args.dll_dir.resolve() / "onnxruntime.dll"))
        asyncio.run(build(args))
