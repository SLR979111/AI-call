"""PCM production, flow control and client-confirmed delivery for a single reply."""
from __future__ import annotations

import asyncio
import struct
import time
from dataclasses import dataclass, field

from .models import Say
from .providers import ProviderError

PCM_HEADER = struct.Struct("<III")
PLAYBACK_WINDOW = 12000  # 500 ms of 24 kHz mono PCM across all units.


@dataclass
class Unit:
    number: int
    say: Say
    entry: dict
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=32))
    sent: int = 0
    played: int = 0
    total: int = 0
    duration_samples: int = 0
    source_done: bool = False
    delivered: bool = False
    finished: asyncio.Event = field(default_factory=asyncio.Event)

    prepared_clause: tuple[str, bytes] | None = None

    def record_cutoff(self):
        """Keep the estimated spoken prefix and the intended continuation apart."""
        if self.delivered:
            return
        text = self.say.text
        duration = self.total if self.source_done else self.duration_samples
        if duration:
            cutoff = len(text) * self.played // duration
        else:
            # A partially synthesized buffer is only a lower bound on duration.
            # Use roughly four Chinese characters per second until EOF is known.
            estimated_samples = max(self.total + 1, len(text) * 6000)
            cutoff = len(text) * self.played // estimated_samples
        self.entry.update(text=text[:cutoff], remaining_text=text[cutoff:],
                          played_ms=round(self.played / 24),
                          delivery="partial" if self.played else "unplayed")

    async def synthesize(self, cloud, pcm: bytes | None, preview, monitor, turn):
        started = time.perf_counter()
        cached = pcm if pcm is not None else getattr(cloud, "cached", lambda _: None)(self.say.text)
        if self.prepared_clause and pcm is None:
            cached = None
        if cached:
            self.duration_samples = len(cached) // 2
        if cached is not None and pcm is None:
            # The utterance is served from the TTS cache without a synthesis
            # request; the speech() path records its own miss/hit otherwise.
            emit = getattr(cloud, "emit_cache_event", None)
            if emit is not None:
                emit("tts", True, started)

        async def chunks():
            if cached is not None:
                for offset in range(0, len(cached), 1920):
                    yield cached[offset:offset + 1920]
            elif self.prepared_clause:
                prefix, clip = self.prepared_clause
                for offset in range(0, len(clip), 1920):
                    yield clip[offset:offset + 1920]
                suffix = self.say.text[len(prefix):]
                if suffix:
                    async for chunk in cloud.speech(suffix):
                        yield chunk
            else:
                source = preview.audio(lambda: cloud.speech(self.say.text)) if preview else cloud.speech(self.say.text)
                async for chunk in source:
                    yield chunk

        first = True
        if monitor and turn:
            monitor.mark_tts_start(turn)
        try:
            async for chunk in chunks():
                if first:
                    first = False
                    if monitor and turn:
                        monitor.mark_tts_first_chunk(
                            cached is not None, turn)
                self.total += len(chunk) // 2
                await self.queue.put(chunk)
            self.source_done = True
            await self.queue.put(None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.queue.put(exc)
        finally:
            if monitor and turn:
                monitor.mark_tts_end(turn)


async def enqueue_for_playback(queue: asyncio.Queue, unit: Unit | None, output: asyncio.Task):
    """Apply backpressure only while a player still consumes this queue."""
    if output.done():
        await output
        return
    if not queue.full():
        queue.put_nowait(unit)
        return
    pending = asyncio.create_task(queue.put(unit))
    try:
        await asyncio.wait((pending, output), return_when=asyncio.FIRST_COMPLETED)
        if output.done():
            await output
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


class Playback:
    """All units in one reply share a window and a receipt event."""

    def __init__(self):
        self.units: dict[int, Unit] = {}
        self.progress = asyncio.Event()
        self.interrupted = False

    def interrupt(self):
        self.interrupted = True
        for unit in self.units.values():
            unit.record_cutoff()
        self.progress.set()

    def acknowledge(self, unit_id: int, samples: int, done: bool) -> Unit | None:
        """Return the unit only when its first final receipt can be committed."""
        unit = self.units.get(unit_id)
        if self.interrupted or unit is None or unit.delivered or not unit.played <= samples <= unit.sent:
            return None
        unit.played = samples
        self.progress.set()
        if done and unit.source_done and samples == unit.total and samples > 0:
            return unit
        if samples:
            unit.entry.update(delivery="partial", played_ms=round(samples / 24))
        return None

    async def packets(self, epoch: int, unit: Unit):
        seq = 0
        while not self.interrupted:
            packet = await unit.queue.get()
            if isinstance(packet, Exception):
                raise packet
            if packet is None:
                break
            while sum(u.sent - u.played for u in self.units.values()) >= PLAYBACK_WINDOW:
                if self.interrupted:
                    return
                self.progress.clear()
                try:
                    await asyncio.wait_for(self.progress.wait(), 5)
                except TimeoutError as exc:
                    raise ProviderError("播放器没有回应，请重新开始通话") from exc
            if self.interrupted:
                return
            unit.sent += len(packet) // 2
            yield PCM_HEADER.pack(epoch, unit.number, seq) + packet
            seq += 1

    async def wait_until_delivered(self):
        try:
            async with asyncio.timeout(6):
                for unit in self.units.values():
                    await unit.finished.wait()
        except TimeoutError as exc:
            raise ProviderError("未收到播放完成回执，请重新开始通话") from exc
