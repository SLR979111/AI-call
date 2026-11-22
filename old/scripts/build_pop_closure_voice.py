"""Render the frozen synthetic merchant recordings with the local Melo provider."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import wave
from contextlib import nullcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.cancel import CancelToken
from app.eval.pop_runner import read_json
from app.voice.melo import MeloTts


async def render(args):
    tts = MeloTts(str(args.model), speed=0.93)
    args.output.mkdir(parents=True, exist_ok=False)
    for file, words in read_json(args.plan)["samples"].items():
        if Path(file).name != file or not file.endswith(".wav"):
            raise ValueError(f"Invalid fixture name {file}")
        samples = b"".join([frame.pcm async for frame in
                            tts.synthesize(words, sample_rate=16000, cancel=CancelToken())])
        with wave.open(str(args.output / file), "wb") as handle:
            handle.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            handle.writeframes(samples)
        print(file, round(len(samples) / 32000, 2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--dll-dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--plan", type=Path, default=ROOT / "eval/pop/closure_voice.v2.json")
    options = parser.parse_args()
    with os.add_dll_directory(str(options.dll_dir)) if options.dll_dir else nullcontext():
        asyncio.run(render(options))
