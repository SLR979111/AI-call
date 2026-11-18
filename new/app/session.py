"""Cancellable conversation loop, with client-confirmed sentence delivery."""

from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import struct
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from .brain import Brain
from .models import Candidate, Memory, Say, TaskCard, ends_with_goodbye, farewell_only
from .providers import Cloud, ProviderError
from .silent_logger import silent_log


PREDICTION_AUDIO_LIMIT_ENV = "PREDICTION_AUDIO_LIMIT"


def prediction_audio_limit_from_env() -> int | None:
    """Number of leading forecast candidates allowed to prepare audio.

    Unset, empty, negative or malformed values mean "no limit", which keeps
    the production behaviour of preparing audio for every candidate. Zero is a
    valid experimental arm: it publishes every candidate as text only and lets
    a reused reply fall back to ordinary TTS.
    """
    raw = os.getenv(PREDICTION_AUDIO_LIMIT_ENV, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


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


class Session:
    def __init__(self, card: TaskCard, cloud: Cloud, brain: Brain,
                 send: Callable[[dict | bytes], Awaitable[None]], *, voice: bool,
                 predictions: bool = True, log_dir: Path | None = None,
                 decision_tracker=None, performance_monitor=None, on_hangup=None):
        self.id = uuid.uuid4().hex[:12]
        self.memory = Memory(card)
        self.cloud, self.brain, self.transport = cloud, brain, send
        self.voice, self.predictions = voice, predictions
        self.epoch = 0
        self.closed = False
        self.ended = False
        self.on_hangup = on_hangup
        self.phase = "ready"
        self.units: dict[int, Unit] = {}
        self.jobs: set[asyncio.Task] = set()
        self.response: asyncio.Task | None = None
        self.forecast: asyncio.Task | None = None
        self.pending_update: asyncio.Task | None = None
        self.candidates: list[Candidate] = []
        self.prediction_audio: dict[str, bytes] = {}
        self.bridge_text = "嗯。"
        self.bridge_audio: bytes | None = None
        self.bridge_delay = 0.35
        self.prediction_epoch = -1
        self.reply_completed = False
        self.starting = False
        self.progress = asyncio.Event()
        self.send_lock = asyncio.Lock()
        self.turn_metrics: dict = {}
        self.log_path = None
        self.decision_tracker = decision_tracker
        self.performance_monitor = performance_monitor
        if log_dir:
            log_dir.mkdir(parents=True, exist_ok=True)
            self.log_path = log_dir / f"{self.id}.jsonl"

    def log(self, event: str, **fields):
        # Write to session log file
        if self.log_path:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"at": time.time(), "event": event, "epoch": self.epoch,
                                         **fields}, ensure_ascii=False) + "\n")
        
        # Also write to silent debug log
        silent_log(event, session_id=self.id, epoch=self.epoch, **fields)

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
            if self.voice:
                self.spawn(self._warm_bridge())
            self.response = self.spawn(self._run(epoch, opening=True))

    def is_current(self, epoch: int):
        return not self.closed and not self.ended and epoch == self.epoch

    async def user_text(self, text: str, *, source: str = "text", end_ms: float | None = None):
        text = text.strip()
        if not text or self.closed or self.ended:
            return
        text = text[:2000]
        
        # Start performance tracking
        if self.performance_monitor:
            self.performance_monitor.start_turn(f"{self.id}-{self.epoch + 1}")
        
        usable = (self.reply_completed and self.prediction_epoch == self.epoch)
        candidates = list(self.candidates) if usable else []
        audio = dict(self.prediction_audio) if usable else {}
        self.prediction_audio.clear()
        await self.interrupt("new_input")
        if self.forecast and not self.forecast.done():
            self.forecast.cancel()
        self.candidates = []
        self.prediction_audio.clear()
        self.prediction_epoch = -1
        self.epoch += 1
        epoch = self.epoch
        self.starting = True
        self.units = {}
        self.reply_completed = False
        self.memory.history.append({"role": "user", "text": text})
        self.turn_metrics = {"input_source": source, "started": time.perf_counter(),
                             "last_voice_client_ms": end_ms, "candidates_available": len(candidates),
                             "reused": False}
        self.log("user", text=text, source=source)
        await self.send({"type": "transcript", "text": text, "final": True, "epoch": self.epoch})
        if self.is_current(epoch):
            self.starting = False
            self.response = self.spawn(self._run(epoch, user=text, candidates=candidates, audio=audio))

    async def interrupt(self, reason: str, *, expected_epoch: int | None = None,
                        evidence: dict | None = None):
        if self.ended:
            return
        if expected_epoch is not None and expected_epoch != self.epoch:
            return
        if (not self.response or self.response.done()) and not self.starting:
            return
        
        # Track interrupt in performance monitor
        if self.performance_monitor:
            self.performance_monitor.mark_interrupt()
        
        old_epoch = self.epoch
        self.epoch += 1
        if self.response:
            self.response.cancel()
        self.response = None
        self.starting = False
        if self.forecast and not self.forecast.done():
            self.forecast.cancel()
        if self.pending_update and not self.pending_update.done():
            # The cached reply was already spoken; an interrupted turn drops
            # its late state update rather than applying it out of order.
            self.log("reuse_update_cancelled", reason=reason)
            self.pending_update.cancel()
        self.pending_update = None
        for unit in self.units.values():
            unit.record_cutoff()
        self.candidates = []
        self.prediction_audio.clear()
        self.prediction_epoch = -1
        self.reply_completed = False
        self.progress.set()
        self.phase = "listening"
        self.log("interrupted", cancelled_epoch=old_epoch, reason=reason,
                 evidence=evidence, cutoff=[u.entry.copy() for u in self.units.values()
                                            if u.entry.get("delivery") == "partial"],
                 **{k: v for k, v in self.turn_metrics.items() if k != "started"})
        await self.send({"type": "cancel", "epoch": old_epoch, "reason": reason})
        await self.state()

    async def playback_progress(self, epoch: int, unit_id: int, samples: int, *, done: bool = False):
        if not self.is_current(epoch):
            return
        unit = self.units.get(unit_id)
        if not unit or "remaining_text" in unit.entry or samples < unit.played or samples > unit.sent:
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

    async def playback_started(self, epoch: int, client_ms: float, unit_id: int | None = None):
        if not math.isfinite(client_ms) or not self.is_current(epoch):
            return
        unit = self.units.get(unit_id)
        if unit_id is not None and unit is None:
            return
        first = "first_playback_client_ms" not in self.turn_metrics
        answer = (unit is not None and not unit.entry.get("bridge")
                  and "answer_playback_client_ms" not in self.turn_metrics)
        if not first and not answer:
            return
        last = self.turn_metrics.get("last_voice_client_ms")
        latency = round(client_ms - last) if last is not None and client_ms >= last else None
        if first:
            self.turn_metrics.update(first_playback_client_ms=client_ms, voice_to_playback_ms=latency)
        if answer:
            self.turn_metrics.update(answer_playback_client_ms=client_ms, voice_to_answer_ms=latency)
            self.log("answer_playback", unit=unit_id, voice_to_answer_ms=latency)
        await self.send({"type": "latency", "epoch": epoch,
                         "voice_to_playback_ms": self.turn_metrics.get("voice_to_playback_ms"),
                         "voice_to_answer_ms": self.turn_metrics.get("voice_to_answer_ms"),
                         "input_source": self.turn_metrics.get("input_source", "opening")})

    async def _synthesize(self, unit: Unit, pcm: bytes | None = None):
        cached = pcm if pcm is not None else getattr(self.cloud, "cached", lambda _: None)(unit.say.text)
        if cached:
            unit.duration_samples = len(cached) // 2

        async def chunks():
            if cached is not None:
                for offset in range(0, len(cached), 1920):
                    yield cached[offset:offset + 1920]
            else:
                async for chunk in self.cloud.speech(unit.say.text):
                    yield chunk

        try:
            async for chunk in chunks():
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
                if self.voice:
                    try:
                        async with asyncio.timeout(6):
                            for pending in self.units.values():
                                await pending.finished.wait()
                    except TimeoutError as exc:
                        raise ProviderError("未收到播放完成回执，请重新开始通话") from exc
                return
            self.phase = "speaking"
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
                while sum(item.sent - item.played for item in self.units.values()) >= 12000:
                    self.progress.clear()
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
                if not unit.entry.get("bridge") and "answer_first_packet_ms" not in self.turn_metrics and "started" in self.turn_metrics:
                    self.turn_metrics["answer_first_packet_ms"] = round(
                        (time.perf_counter() - self.turn_metrics["started"]) * 1000)
                if "first_packet_ms" not in self.turn_metrics and "started" in self.turn_metrics:
                    self.turn_metrics["first_packet_ms"] = round(
                        (time.perf_counter() - self.turn_metrics["started"]) * 1000)
            if not self.is_current(epoch):
                return
            await self.send({"type": "utterance_end", "epoch": epoch,
                             "unit": unit.number, "samples": unit.total})
            # The player schedules the next unit behind this one. Overlap its
            # transfer with playback while keeping one shared 500 ms window.

    async def _run(self, epoch: int, *, opening: bool = False, user: str = "",
                   candidates: list[Candidate] | None = None,
                   audio: dict[str, bytes] | None = None):
        queue: asyncio.Queue = asyncio.Queue(maxsize=5)
        children: list[asyncio.Task] = []
        output = self.spawn(self._output(epoch, queue))
        children.append(output)
        before = set(self.memory.delivered)
        current_units: list[Unit] = []
        pending_update: asyncio.Task | None = None
        hangup = False

        first_answer = asyncio.Event()

        async def bridge():
            try:
                await asyncio.wait_for(first_answer.wait(), self.bridge_delay)
                return
            except TimeoutError:
                pass
            if self.bridge_audio and self.is_current(epoch) and not first_answer.is_set():
                self.turn_metrics["bridge_used"] = True
                await say(Say(text=self.bridge_text), bridge_pcm=self.bridge_audio)

        async def say(line: Say, *, bridge_pcm: bytes | None = None, candidate_id: str | None = None):
            if not self.is_current(epoch):
                raise asyncio.CancelledError()
            entry = {"role": "assistant", "text": line.text, "delivery": "unplayed"}
            if bridge_pcm is not None:
                entry["bridge"] = True
            self.memory.history.append(entry)
            unit = Unit(len(current_units) + 1, line, entry)
            current_units.append(unit)
            self.units[unit.number] = unit
            if self.voice:
                pcm = bridge_pcm if bridge_pcm is not None else (audio or {}).get(line.text)
                source = "bridge_pcm" if bridge_pcm is not None else "prediction_pcm" if pcm else "tts"
                if bridge_pcm is None and "prediction_audio_hit" not in self.turn_metrics:
                    self.turn_metrics["prediction_audio_hit"] = bool(pcm)
                    self.turn_metrics["prediction_audio_bytes"] = len(pcm) if pcm else 0
                self.log("audio_source", unit=unit.number, source=source,
                         audio_bytes=len(pcm) if pcm else 0)
                children.append(self.spawn(self._synthesize(unit, pcm)))
            await self.send({"type": "utterance", "epoch": epoch, "unit": unit.number,
                             "text": line.text, "rate": 24000, "candidate_id": candidate_id,
                             "filler": bridge_pcm is not None})
            await queue.put(unit)

        try:
            self.phase = "thinking"
            await self.send({"type": "reply_start", "epoch": epoch})
            await self.state()
            if opening:
                opening_text = self.memory.card.opening
                question = opening_text.split("。")[-1] if opening_text.endswith(("？", "?")) else ""
                await say(Say(text=opening_text, question=question))
            elif self.memory.closing_reason(user):
                if self.memory.status != "complete":
                    self.memory.status = "paused"
                hangup = True
                await say(Say(text="好，再见。"))
            else:
                if self.voice and self.bridge_audio:
                    children.append(self.spawn(bridge()))
                async for kind, value in self.brain.respond(self.memory, candidates or []):
                    if not self.is_current(epoch):
                        return
                    if output.done() and output.exception():
                        raise output.exception()
                    if kind in {"say", "reuse"}:
                        first_answer.set()
                        # Track decision in decision tracker
                        if self.decision_tracker:
                            if kind == "reuse":
                                self.turn_metrics["reused"] = True
                                candidate_id = next((c.id for c in (candidates or []) if c.reply.text == value.text), "unknown")
                                self.decision_tracker.record_reuse(
                                    candidate_id,
                                    (time.perf_counter() - self.turn_metrics["started"]) * 1000
                                )
                            else:
                                self.decision_tracker.record_say(
                                    (time.perf_counter() - self.turn_metrics["started"]) * 1000
                                )
                        
                        if kind == "reuse":
                            self.turn_metrics["reused"] = True
                        if "first_text_ms" not in self.turn_metrics:
                            self.turn_metrics["first_text_ms"] = round(
                                (time.perf_counter() - self.turn_metrics["started"]) * 1000)
                            await self.send({"type": "response_source", "epoch": epoch,
                                             "source": "prediction" if kind == "reuse" else "direct"})
                        candidate_id = next((c.id for c in candidates or [] if c.reply.text == value.text), None) if kind == "reuse" else None
                        await say(value, candidate_id=candidate_id)
                    elif kind == "update_async":
                        # Do not await the update here: the cached audio is
                        # already playing and the update must not hold it up.
                        if pending_update is None:
                            pending_update = self.spawn(
                                self.brain.update_for_reuse(self.memory, value))
                            self.pending_update = pending_update
                    else:
                        self.memory.apply(value, user, epoch, before)
                        await self.state()
                last_reply = current_units[-1].say
                hangup = (self.memory.ready_to_say_goodbye(user, last_reply)
                          or self.memory.status in {"paused", "declined"})
                if ends_with_goodbye(user) and not farewell_only(user) and ends_with_goodbye(last_reply.text):
                    if self.memory.status == "active":
                        self.memory.status = "paused"
                    hangup = True
                if hangup and not ends_with_goodbye(last_reply.text):
                    await say(Say(text="再见。"))
            await queue.put(None)
            # Forecast the next user reply while this response is playing. It
            # only becomes eligible after ALL of these units finish playback.
            if self.predictions and self.memory.status == "active":
                snapshot = copy.deepcopy(self.brain.snapshot(self.memory))
                snapshot["opening"] = opening
                snapshot["pending_reply"] = [u.say.model_dump() for u in current_units]
                self.forecast = self.spawn(self._predict(epoch, snapshot))
            await output
            if self.is_current(epoch):
                # End performance tracking
                if self.performance_monitor:
                    self.performance_monitor.end_turn()
                
                self.reply_completed = all(unit.delivered for unit in current_units)
                self.phase = "listening"
                self.log("reply_finished", **{k: v for k, v in self.turn_metrics.items() if k != "started"})
                
                # Track task completion
                if self.memory.status == "complete" and self.performance_monitor:
                    self.performance_monitor.record_task_completion(success=True)
                
                await self.send({"type": "reply_done", "epoch": epoch,
                                 "reused": self.turn_metrics.get("reused", False)})
                await self.state()
                if pending_update is not None:
                    try:
                        update = await pending_update
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.log("reuse_update_error", error=type(exc).__name__)
                    else:
                        if self.is_current(epoch):
                            self.memory.apply(update, user, epoch, before)
                            await self.state()
                    finally:
                        if self.pending_update is pending_update:
                            self.pending_update = None
                if hangup and self.reply_completed and self.is_current(epoch):
                    self.ended = True
                    self.phase = "ended"
                    await self.send({"type": "hangup", "epoch": epoch, "reason": self.memory.status})
                    if self.on_hangup:
                        await self.on_hangup()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.is_current(epoch):
                for unit in current_units:
                    unit.record_cutoff()
                # Track error in performance monitor
                if self.performance_monitor:
                    self.performance_monitor.record_error(type(exc).__name__)
                
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
            if pending_update is not None:
                if not pending_update.done():
                    pending_update.cancel()
                await asyncio.gather(pending_update, return_exceptions=True)
            if self.pending_update is pending_update:
                self.pending_update = None
            for child in children:
                if not child.done():
                    child.cancel()
            await asyncio.gather(*children, return_exceptions=True)

    async def _warm_bridge(self):
        try:
            pcm = await self.cloud.prefetch(self.bridge_text)
            # Never put a long filler ahead of a ready answer. At 24 kHz
            # mono PCM16, 24,000 bytes is 500 ms. Skip rather than truncate.
            if not self.closed and pcm and len(pcm) <= 24000:
                self.bridge_audio = pcm
            elif pcm:
                self.log("bridge_unavailable", reason="audio_too_long", audio_bytes=len(pcm))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log("bridge_unavailable", error=type(exc).__name__)

    async def _predict(self, epoch: int, snapshot: dict):
        started = time.perf_counter()
        limit = prediction_audio_limit_from_env()
        if limit is not None:
            self.log("prediction_audio_limit", limit=limit)
        try:
            ready: list[tuple[int, Candidate]] = []
            tasks = []
            audio_tasks: dict[str, asyncio.Task] = {}
            received = 0

            async def prepare(item, *, audio_allowed: bool, index: int):
                text_ready_ms = round((time.perf_counter() - started) * 1000)
                audio_started = time.perf_counter()
                pcm = None
                audio_status = "not_requested"
                if self.voice:
                    if not audio_allowed:
                        # Beyond the experimental budget the text candidate is
                        # still useful, but selecting it will use ordinary TTS.
                        audio_status = "limit_skipped"
                        self.log("prediction_audio_skipped", candidate_id=item.id,
                                 limit=limit, text_ready_ms=text_ready_ms)
                    else:
                        text = item.reply.text
                        if text not in audio_tasks:
                            audio_tasks[text] = asyncio.create_task(self.cloud.prefetch(text))
                        pcm = await asyncio.shield(audio_tasks[text])
                        if not pcm:
                            # Without prepared audio this is not a cache hit: the
                            # reply would fall back to a blocking TTS request.
                            self.log("prediction_audio_miss", candidate_id=item.id,
                                     text_ready_ms=text_ready_ms)
                            return
                        audio_status = "prefetched"
                if self.is_current(epoch):
                    if pcm:
                        self.prediction_audio[item.reply.text] = pcm
                    ready.append((index, item))
                    # With no budget this keeps the historical publish order
                    # (audio completion). The experimental arms sort by the
                    # order candidates arrived so a text-only candidate cannot
                    # jump ahead of an earlier one that is still synthesizing.
                    ordered = ready if limit is None else sorted(ready, key=lambda entry: entry[0])
                    self.candidates = [candidate for _, candidate in ordered]
                    self.prediction_epoch = epoch
                    await self.send({"type": "candidates", "epoch": epoch, "items": [
                        {"id": candidate.id, "text": candidate.reply.text}
                        for _, candidate in ordered]})
                    self.log("prediction_ready", count=len(ready), candidate_id=item.id,
                             text_ready_ms=text_ready_ms,
                             audio_prepare_ms=round((time.perf_counter() - audio_started) * 1000),
                             ready_ms=round((time.perf_counter() - started) * 1000),
                             audio_bytes=len(pcm) if pcm else 0,
                             audio_status=audio_status,
                             candidates=[candidate.model_dump() for _, candidate in ordered])

            try:
                if hasattr(self.brain, "predict_batches"):
                    async for batch in self.brain.predict_batches(snapshot):
                        if not self.is_current(epoch):
                            return
                        for item in batch:
                            received += 1
                            tasks.append(asyncio.create_task(prepare(
                                item, audio_allowed=limit is None or received <= limit,
                                index=received)))
                else:
                    batch = await self.brain.predict(snapshot)
                    for item in batch:
                        received += 1
                        tasks.append(asyncio.create_task(prepare(
                            item, audio_allowed=limit is None or received <= limit,
                            index=received)))
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception):
                        self.log("prediction_miss", error=type(result).__name__)
            finally:
                pending = tasks + list(audio_tasks.values())
                for task in pending:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
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
        self.candidates.clear()
        self.prediction_audio.clear()
        self.bridge_audio = None
        self.log("closed")
