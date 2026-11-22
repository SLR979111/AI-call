"""Prepare only the first spoken sentence before a cloud call."""
from __future__ import annotations

import asyncio
import logging
import os

from .providers import Cloud
from .speech_start import first_sentence


class TaskPrewarmer:
    def __init__(self):
        self.active: dict[str, asyncio.Task] = {}
        self.failed: set[str] = set()

    async def prepare(self, opening: str, cloud: Cloud, *, wait=False, retry=False) -> dict:
        if os.getenv("TASK_PREWARM_ON_LOAD", "1").strip().lower() in {"0", "false", "off", "no", "disabled"}:
            return {"started": False, "status": "disabled"}
        text = first_sentence(opening)
        if not text:
            return {"started": False, "status": "failed"}
        if cloud.cached(text):
            self.failed.discard(text)
            return {"started": False, "status": "ready"}
        if text in self.failed and not retry:
            return {"started": False, "status": "failed"}
        task = self.active.get(text)
        if task is None:
            self.failed.discard(text)
            task = asyncio.create_task(self._prepare(text, cloud))
            self.active[text] = task
        return {"started": True, "status": await asyncio.shield(task) if wait else "preparing"}

    async def _prepare(self, text: str, cloud: Cloud) -> str:
        try:
            if await cloud.warm(text):
                return "ready"
        except Exception:
            logging.getLogger(__name__).exception("Opening audio preparation failed")
        finally:
            self.active.pop(text, None)
        self.failed.add(text)
        return "failed"

    async def close(self):
        tasks = list(self.active.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.failed.clear()
