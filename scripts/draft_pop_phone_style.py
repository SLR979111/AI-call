"""Draft POP telephone templates offline for review."""
import argparse
import asyncio
from pathlib import Path
import sys

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.pop_grounded_answer import chat_json
from app.agent.pop_phone_style import BRIEFS, PROMPT, REQUIRED, SOURCES
from app.eval.pop_judge import JudgeConfig
from app.eval.pop_runner import write_json


async def draft(output):
    config = JudgeConfig.from_env()
    async with httpx.AsyncClient(timeout=25, trust_env=False) as client:
        slots = asyncio.Semaphore(3)

        async def one(key):
            async with slots:
                raw = await chat_json(client, config.endpoint, config.model, config.api_key, PROMPT,
                                      {"本轮目的": BRIEFS[key], "必须保留": REQUIRED[key]},
                                      max_tokens=180, temperature=0.5)
            lines = [raw.get("a"), raw.get("b")]
            if not all(isinstance(line, str) for line in lines) or len(set(lines) - {SOURCES[key]}) != 2:
                raise ValueError(f"Model did not return distinct alternatives for {key}")
            return key, lines

        replies = dict(await asyncio.gather(*(one(key) for key in SOURCES)))
    write_json(output, {"model": config.model, "replies": replies})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    asyncio.run(draft(parser.parse_args().output))
