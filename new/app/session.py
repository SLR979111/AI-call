"""Cancellable conversation loop, with client-confirmed sentence delivery."""

from __future__ import annotations

import asyncio
import copy
import json
import math
import struct
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from .brain import Brain
from .models import Candidate, Memory, Say, TaskCard
from .providers import Cloud, ProviderError


@dataclass
class Unit:
    number: int
    say: Say
    entry: dict
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=32))
    sent: int = 0
    played: int = 0
    total: int = 0
    source_done: bool = False
    delivered: bool = False
    finished: asyncio.Event = field(default_factory=asyncio.Event)


class Session:
    def __init__(self, card: TaskCard, cloud: Cloud, brain: Brain,
                 send: Callable[[dict | bytes], Awaitable[None]], *, voice: bool,
                 predictions: bool = True, log_dir: Path | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.memory = Memory(card)
        self.cloud, self.brain, self.transport = cloud, brain, send
        self.voice, self.predictions = voice, predictions
        self.epoch = 0
        self.closed = False
        self.phase = "ready"
        self.units: dict[int, Unit] = {}
        self.jobs: set[asyncio.Task] = set()
        self.response: asyncio.Task | None = None
        self.forecast: asyncio.Task | None = None
        self.candidates: list[Candidate] = []
        self.prediction_epoch = -1
        self.reply_completed = False
        self.starting = False
        self.progress = asyncio.Event()
        self.send_lock = asyncio.Lock()
        self.turn_metrics: dict = {}
        self.log_path = None
        if log_dir:
            log_dir.mkdir(parents=True, exist_ok=True)
            self.log_path = log_dir / f"{self.id}.jsonl"

    def log(self, event: str, **fields):
        if self.log_path:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"at": time.time(), "event": event, "epoch": self.epoch,
                                         **fields}, ensure_ascii=False) + "\n")

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.jobs.add(task)

        def done(job):
            self.jobs.discard(job)
            if not job.cancelled():
                error = job.exception()
                if error:
                    self.log("background_error", error=type(error).__name__)
        task.add_done_callback(done)
        return task

    async def send(self, payload: dict | bytes):
        if self.closed:
            return
        async with self.send_lock:
            if not self.closed:
                await self.transport(payload)

    async def state(self):
        await self.send({"type": "state", "epoch": self.epoch, "phase": self.phase,
                         **self.memory.view()})

    async def start(self):
        self.epoch += 1
        self.log("started", task=self.memory.card.model_dump(), voice=self.voice,
                 predictions=self.predictions)
        epoch = self.epoch
        self.starting = True
        await self.send({"type": "ready", "session_id": self.id, "voice": self.voice,
                         "predictions": self.predictions, "task": self.memory.card.model_dump()})
        if self.is_current(epoch):
            self.starting = False
            self.response = self.spawn(self._run(epoch, opening=True))

    def is_current(self, epoch: int):
        return not self.closed and epoch == self.epoch

    async def user_text(self, text: str, *, source: str = "text", end_ms: float | None = None):
        text = text.strip()
        if not text or self.closed:
            return
        text = text[:2000]
        usable = (self.reply_completed and self.prediction_epoch == self.epoch)
        candidates = list(self.candidates) if usable else []
        await self.interrupt("new_input")
        if self.forecast and not self.forecast.done():
            self.forecast.cancel()
        self.candidates = []
        self.prediction_epoch = -1
        self.epoch += 1
        epoch = self.epoch
        self.starting = True
        self.units = {}
        self.reply_completed = False
        self.memory.status = "active"
        self.memory.history.append({"role": "user", "text": text})
        self.turn_metrics = {"input_source": source, "started": time.perf_counter(),
                             "last_voice_client_ms": end_ms, "candidates_available": len(candidates),
                             "reused": False}
        self.log("user", text=text, source=source)
        await self.send({"type": "transcript", "text": text, "final": True, "epoch": self.epoch})
        if self.is_current(epoch):
            self.starting = False
            self.response = self.spawn(self._run(epoch, user=text, candidates=candidates))

    async def interrupt(self, reason: str, *, expected_epoch: int | None = None,
                        evidence: dict | None = None):
        if expected_epoch is not None and expected_epoch != self.epoch:
            return
        if (not self.response or self.response.done()) and not self.starting:
            return
        old_epoch = self.epoch
        self.epoch += 1
        if self.response:
            self.response.cancel()
        self.response = None
        self.starting = False
        if self.forecast and not self.forecast.done():
            self.forecast.cancel()
        for unit in self.units.values():
            if not unit.delivered:
                unit.entry["delivery"] = "partial" if unit.played else "unplayed"
                unit.entry["played_ms"] = round(unit.played / 24)
        self.candidates = []
        self.prediction_epoch = -1
        self.reply_completed = False
        self.progress.set()
        self.phase = "listening"
        self.log("interrupted", cancelled_epoch=old_epoch, reason=reason,
                 evidence=evidence,
                 **{k: v for k, v in self.turn_metrics.items() if k != "started"})
        await self.send({"type": "cancel", "epoch": old_epoch, "reason": reason})
        await self.state()

    async def playback_progress(self, epoch: int, unit_id: int, samples: int, *, done: bool = False):
        if not self.is_current(epoch):
            return
        unit = self.units.get(unit_id)
        if not unit or samples < unit.played or samples > unit.sent:
            return
        unit.played = samples
        self.progress.set()
        if samples and not unit.delivered:
            unit.entry["delivery"] = "partial"
            unit.entry["played_ms"] = round(samples / 24)
        if done and unit.source_done and samples == unit.total and unit.total > 0:
            self._delivered(unit)
            await self.state()

    def _delivered(self, unit: Unit):
        if unit.delivered:
            return
        unit.delivered = True
        unit.entry["delivery"] = "complete"
        unit.entry.pop("played_ms", None)
        self.memory.delivered.update(unit.say.covers)
        self.memory.last_question = unit.say.question
        unit.finished.set()
        self.log("delivered", unit=unit.number, text=unit.say.text, covers=unit.say.covers)

    async def playback_started(self, epoch: int, client_ms: float):
        if not math.isfinite(client_ms) or not self.is_current(epoch) or "first_playback_client_ms" in self.turn_metrics:
            return
        self.turn_metrics["first_playback_client_ms"] = client_ms
        last = self.turn_metrics.get("last_voice_client_ms")
        if last is not None and client_ms >= last:
            self.turn_metrics["voice_to_playback_ms"] = round(client_ms - last)
        await self.send({"type": "latency", "epoch": epoch,
                         "voice_to_playback_ms": self.turn_metrics.get("voice_to_playback_ms"),
                         "input_source": self.turn_metrics.get("input_source", "opening")})

    async def _synthesize(self, unit: Unit):
        try:
            async for chunk in self.cloud.speech(unit.say.text):
                unit.total += len(chunk) // 2
                await unit.queue.put(chunk)
            unit.source_done = True
            await unit.queue.put(None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await unit.queue.put(exc)

    async def _output(self, epoch: int, queue: asyncio.Queue):
        while self.is_current(epoch):
            unit = await queue.get()
            if unit is None:
                return
            self.phase = "speaking"
            await self.send({"type": "utterance", "epoch": epoch, "unit": unit.number,
                             "text": unit.say.text, "rate": 24000})
            await self.state()
            if not self.voice:
                self._delivered(unit)
                continue
            seq = 0
            while self.is_current(epoch):
                packet = await unit.queue.get()
                if isinstance(packet, Exception):
                    raise packet
                if packet is None:
                    break
                # At most ~500 ms of unplayed sound in flight. Control input is
                # processed by the independent websocket reader throughout.
                while unit.sent - unit.played >= 12000:
                    self.progress.clear()
                    if unit.sent - unit.played < 12000:
                        break
                    try:
                        await asyncio.wait_for(self.progress.wait(), 5)
                    except TimeoutError as exc:
                        raise ProviderError("播放器没有回应，请重新开始通话") from exc
                    if not self.is_current(epoch):
                        return
                if not self.is_current(epoch):
                    return
                await self.send(struct.pack("<III", epoch, unit.number, seq) + packet)
                seq += 1
                unit.sent += len(packet) // 2
                if "first_packet_ms" not in self.turn_metrics and "started" in self.turn_metrics:
                    self.turn_metrics["first_packet_ms"] = round(
                        (time.perf_counter() - self.turn_metrics["started"]) * 1000)
            if not self.is_current(epoch):
                return
            await self.send({"type": "utterance_end", "epoch": epoch,
                             "unit": unit.number, "samples": unit.total})
            try:
                await asyncio.wait_for(unit.finished.wait(), 6)
            except TimeoutError as exc:
                raise ProviderError("未收到播放完成回执，请重新开始通话") from exc

    async def _run(self, epoch: int, *, opening: bool = False, user: str = "",
                   candidates: list[Candidate] | None = None):
        queue: asyncio.Queue = asyncio.Queue(maxsize=3)
        children: list[asyncio.Task] = []
        output = self.spawn(self._output(epoch, queue))
        children.append(output)
        before = set(self.memory.delivered)
        current_units: list[Unit] = []

        async def say(line: Say):
            if not self.is_current(epoch):
                raise asyncio.CancelledError()
            entry = {"role": "assistant", "text": line.text, "delivery": "unplayed"}
            self.memory.history.append(entry)
            unit = Unit(len(current_units) + 1, line, entry)
            current_units.append(unit)
            self.units[unit.number] = unit
            if self.voice:
                children.append(self.spawn(self._synthesize(unit)))
            await queue.put(unit)

        try:
            self.phase = "thinking"
            await self.send({"type": "reply_start", "epoch": epoch})
            await self.state()
            if opening:
                opening_text = self.memory.card.opening
                question = opening_text.split("。")[-1] if opening_text.endswith(("？", "?")) else ""
                await say(Say(text=opening_text, question=question))
            else:
                async for kind, value in self.brain.respond(self.memory, candidates or []):
                    if not self.is_current(epoch):
                        return
                    if output.done() and output.exception():
                        raise output.exception()
                    if kind in {"say", "reuse"}:
                        if kind == "reuse":
                            self.turn_metrics["reused"] = True
                        if "first_text_ms" not in self.turn_metrics:
                            self.turn_metrics["first_text_ms"] = round(
                                (time.perf_counter() - self.turn_metrics["started"]) * 1000)
                            await self.send({"type": "response_source", "epoch": epoch,
                                             "source": "prediction" if kind == "reuse" else "direct"})
                        await say(value)
                    else:
                        self.memory.apply(value, user, epoch, before)
                        await self.state()
            await queue.put(None)
            # Forecast the next user reply while this response is playing. It
            # only becomes eligible after ALL of these units finish playback.
            if self.predictions and self.memory.status == "active":
                snapshot = copy.deepcopy(self.brain.snapshot(self.memory))
                snapshot["pending_reply"] = [u.say.model_dump() for u in current_units]
                self.forecast = self.spawn(self._predict(epoch, snapshot))
            await output
            if self.is_current(epoch):
                self.reply_completed = all(unit.delivered for unit in current_units)
                self.phase = "listening"
                self.log("reply_finished", **{k: v for k, v in self.turn_metrics.items() if k != "started"})
                await self.send({"type": "reply_done", "epoch": epoch,
                                 "reused": self.turn_metrics.get("reused", False)})
                await self.state()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.is_current(epoch):
                self.reply_completed = False
                self.candidates = []
                if self.forecast and not self.forecast.done():
                    self.forecast.cancel()
                self.phase = "listening"
                message = str(exc) if isinstance(exc, ProviderError) else "本次回复未完成，请重试或换一种说法。"
                self.log("reply_error", error=type(exc).__name__)
                await self.send({"type": "cancel", "epoch": epoch, "reason": "reply_error"})
                await self.send({"type": "error", "message": message, "recoverable": True})
                await self.state()
        finally:
            for child in children:
                if not child.done():
                    child.cancel()
            await asyncio.gather(*children, return_exceptions=True)

    async def _predict(self, epoch: int, snapshot: dict):
        try:
            candidates = await self.brain.predict(snapshot)
            ready = []
            for item in candidates:
                if not self.is_current(epoch):
                    return
                if self.voice:
                    await self.cloud.prefetch(item.reply.text)
                ready.append(item)
                if self.is_current(epoch):
                    self.candidates = list(ready)
                    self.prediction_epoch = epoch
            self.log("prediction_ready", count=len(ready),
                     candidates=[item.model_dump() for item in ready])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Forecasts are expendable; a failure never blocks a response.
            self.log("prediction_miss", error=type(exc).__name__)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.epoch += 1
        for job in list(self.jobs):
            job.cancel()
        if self.jobs:
            await asyncio.gather(*list(self.jobs), return_exceptions=True)
        self.log("closed")
