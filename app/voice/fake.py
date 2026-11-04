"""Explicit test provider: deterministic tone, never represented as speech."""
import asyncio
import math
import struct

from .types import AsrEvent, AudioChunk, mono_ms


class FakeTts:
    sample_rate = 24000

    async def synthesize(self, text, *, voice="default", sample_rate=24000, cancel):
        cancel.check()
        await asyncio.sleep(0)
        pcm = b"".join(struct.pack("<h", int(400 * math.sin(i * 2 * math.pi * 440 / sample_rate)))
                       for i in range(sample_rate // 10))
        cancel.check()
        yield AudioChunk(pcm, 0, is_last=True, ts_mono_ms=mono_ms(), sample_rate=sample_rate)


class FakeAsrStream:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.closed = False

    async def push(self, frame):
        pass

    async def finish(self):
        if not self.closed:
            self.queue.put_nowait(AsrEvent("final"))
            await self.close()

    async def events(self):
        while (item := await self.queue.get()) is not None:
            yield item

    async def close(self):
        if not self.closed:
            self.closed = True
            self.queue.put_nowait(None)


class FakeAsr:
    async def start(self, session_id, *, sample_rate=16000, hotwords=None):
        return FakeAsrStream()
