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
from .models import Candidate, Memory, Say, TaskCard, ends_with_goodbye, farewell_only
from .providers import Cloud, ProviderError
from .silent_logger import silent_log


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
        # Forecasts prepared for the turn a speech onset interrupted. They are
        # only candidates: the next user turn still has to pass the content
        # rules, and they expire as soon as another turn starts.
        self.staged_candidates: list[Candidate] = []
        self.staged_audio: dict[str, bytes] = {}
        self.staged_epoch = -1
        self.bridge_text = "嗯。"
        self.bridge_audio: bytes | None = None
        self.bridge_delay = 0.35
        self.prediction_epoch = -1
        self.reply_completed = False
        self.starting = False
        self.progress = asyncio.Event()
        self.send_lock = asyncio.Lock()
        self.turn_metrics: dict = {}
        self.turn = None  # LatencyBreakdown for the current turn
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

    def _begin_turn(self, source: str):
        """Reset one-turn state and start the performance turn object."""
        self.turn_metrics = {"input_source": source, "started": time.perf_counter(),
                             "candidates_available": 0, "reused": False,
                             "reuse_path": "opening" if source == "opening" else "direct"}
        self.turn = None
        if self.performance_monitor:
            self.turn = self.performance_monitor.start_turn(f"{self.id}-{self.epoch}")

    def _finish_turn(self, *, completed: bool, error: str | None = None,
                     turn=None):
        """Copy session metrics onto the performance turn and close it once."""
        target = turn if turn is not None else self.turn
        if target is None:
            return
        metrics = self.turn_metrics
        target.reused = bool(metrics.get("reused", False))
        target.source = str(metrics.get("input_source", "unknown"))
        target.path = metrics.get("reuse_path")
        target.candidates_available = int(metrics.get("candidates_available") or 0)
        target.prediction_audio_bytes = int(metrics.get("prediction_audio_bytes") or 0)
        target.prediction_audio_hit = metrics.get("prediction_audio_hit")
        for name in ("first_text_ms", "first_packet_ms", "answer_first_packet_ms",
                     "voice_to_playback_ms", "voice_to_answer_ms"):
            value = metrics.get(name)
            if isinstance(value, (int, float)):
                setattr(target, name, float(value))
        target.completed = (completed and error is None
                            and not metrics.get("interrupted", False))
        if self.performance_monitor:
            self.performance_monitor.end_turn(target)
        candidate_id = metrics.get("reuse_candidate_id")
        if (self.decision_tracker and candidate_id
                and not metrics.get("reuse_outcome_recorded")):
            metrics["reuse_outcome_recorded"] = True
            self.decision_tracker.record_reuse_outcome(candidate_id,
                                                       target.completed)
        if self.turn is target:
            self.turn = None

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
            self._begin_turn("opening")
            if self.voice:
                self.spawn(self._warm_bridge())
            self.response = self.spawn(self._run(epoch, opening=True))

    def is_current(self, epoch: int):
        return not self.closed and not self.ended and epoch == self.epoch

    def _clear_staged(self):
        self.staged_candidates = []
        self.staged_audio = {}
        self.staged_epoch = -1

    def _stage_predictions(self, epoch: int) -> bool:
        """Keep forecasts that were ready for the turn a speech onset interrupted.

        A speech onset consumes the turn before its transcript arrives. The
        prepared replies and PCM are still valid for the immediately following
        user turn, but only the deterministic content rules may select them.
        """
        if self.prediction_epoch != epoch or not self.candidates:
            return False
        if self.voice:
            candidates = [c for c in self.candidates
                          if c.reply.text in self.prediction_audio]
            audio = {c.reply.text: self.prediction_audio[c.reply.text]
                     for c in candidates}
        else:
            candidates = list(self.candidates)
            audio = {}
        if not candidates:
            return False
        self.staged_candidates = candidates
        self.staged_audio = audio
        self.staged_epoch = epoch
        self.log("predictions_staged", interrupted_epoch=epoch, candidates=len(candidates),
                 audio_bytes=sum(len(pcm) for pcm in audio.values()))
        return True

    def _take_staged(self):
        """Consume forecasts only for the turn that immediately follows them."""
        if not self.staged_candidates or self.staged_epoch != self.epoch - 1:
            self._clear_staged()
            return None
        candidates, audio = self.staged_candidates, self.staged_audio
        self._clear_staged()
        return candidates, audio

    def _confirmed_staged(self, candidates: list[Candidate]) -> Candidate | None:
        """Reuse a staged forecast only when the content rules select it."""
        if not candidates:
            return None
        matcher = getattr(self.brain, "match_reuse", None)
        if matcher is None:
            return None
        try:
            return matcher(self.memory, candidates)
        except Exception as exc:
            self.log("staged_reuse_error", error=type(exc).__name__)
            return None

    async def user_text(self, text: str, *, source: str = "text", end_ms: float | None = None):
        text = text.strip()
        if not text or self.closed or self.ended:
            return
        text = text[:2000]

        usable = (self.reply_completed and self.prediction_epoch == self.epoch)
        candidates = list(self.candidates) if usable else []
        audio = dict(self.prediction_audio) if usable else {}
        staged = None
        if usable:
            self._clear_staged()
        else:
            # A speech onset cleared the live slot; the forecasts it interrupted
            # may still answer this next user turn.
            staged = self._take_staged()
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
        staged_reuse = False
        if not usable and staged is not None:
            staged_candidates, staged_audio = staged
            if self.voice:
                staged_candidates = [c for c in staged_candidates
                                     if c.reply.text in staged_audio]
            confirmed = self._confirmed_staged(staged_candidates)
            if confirmed is not None:
                candidates = [confirmed]
                audio = ({confirmed.reply.text: staged_audio[confirmed.reply.text]}
                         if self.voice else {})
                staged_reuse = True
                self.log("predictions_reused_after_interrupt", candidate_id=confirmed.id)
            else:
                candidates, audio = [], {}
                self.log("predictions_dropped_after_interrupt",
                         candidates=len(staged_candidates))
        self._begin_turn(source)
        self.turn_metrics.update(last_voice_client_ms=end_ms,
                                 candidates_available=len(candidates),
                                 staged_reuse=staged_reuse)
        self.log("user", text=text, source=source, staged_reuse=staged_reuse)
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
        # Barge-in is the one interrupt that can still serve the next user
        # turn with already prepared replies. Any other reason discards them,
        # and the staged set never survives a second turn boundary.
        if reason == "speech":
            staged = self._stage_predictions(old_epoch)
        else:
            self._clear_staged()
            staged = False
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
        self.turn_metrics["interrupted"] = True
        self.log("interrupted", cancelled_epoch=old_epoch, reason=reason,
                 evidence=evidence, staged=staged,
                 staged_candidates=len(self.staged_candidates),
                 cutoff=[u.entry.copy() for u in self.units.values()
                         if u.entry.get("delivery") == "partial"],
                 **{k: v for k, v in self.turn_metrics.items() if k != "started"})
        self._finish_turn(completed=False)
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

    async def _synthesize(self, unit: Unit, pcm: bytes | None = None, *,
                          turn=None):
        turn = turn if turn is not None else self.turn
        started = time.perf_counter()
        cached = pcm if pcm is not None else getattr(self.cloud, "cached", lambda _: None)(unit.say.text)
        if cached:
            unit.duration_samples = len(cached) // 2
        if cached is not None and pcm is None:
            # The utterance is served from the TTS cache without a synthesis
            # request; the speech() path records its own miss/hit otherwise.
            emit = getattr(self.cloud, "emit_cache_event", None)
            if emit is not None:
                emit("tts", True, started)

        async def chunks():
            if cached is not None:
                for offset in range(0, len(cached), 1920):
                    yield cached[offset:offset + 1920]
            else:
                async for chunk in self.cloud.speech(unit.say.text):
                    yield chunk

        first = True
        if self.performance_monitor and turn:
            self.performance_monitor.mark_tts_start(turn)
        try:
            async for chunk in chunks():
                if first:
                    first = False
                    if self.performance_monitor and turn:
                        self.performance_monitor.mark_tts_first_chunk(
                            cached is not None, turn)
                unit.total += len(chunk) // 2
                await unit.queue.put(chunk)
            unit.source_done = True
            await unit.queue.put(None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await unit.queue.put(exc)
        finally:
            if self.performance_monitor and turn:
                self.performance_monitor.mark_tts_end(turn)

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
                if (not unit.entry.get("bridge")
                        and "answer_first_packet_ms" not in self.turn_metrics
                        and "started" in self.turn_metrics
                        and self.turn_metrics.get("input_source") != "opening"):
                    self.turn_metrics["answer_first_packet_ms"] = round(
                        (time.perf_counter() - self.turn_metrics["started"]) * 1000)
                if ("first_packet_ms" not in self.turn_metrics
                        and "started" in self.turn_metrics
                        and self.turn_metrics.get("input_source") != "opening"):
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
        turn = self.turn
        llm_started = False
        llm_first = False

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
                    if pcm and self.decision_tracker:
                        # Match the single prediction_audio_bytes field logged
                        # for the turn, so stats and logs share one definition.
                        self.decision_tracker.record_prediction_audio(
                            used_bytes=len(pcm))
                self.log("audio_source", unit=unit.number, source=source,
                         audio_bytes=len(pcm) if pcm else 0)
                children.append(self.spawn(self._synthesize(unit, pcm, turn=turn)))
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
                if self.performance_monitor and turn:
                    self.performance_monitor.mark_llm_start(turn)
                    llm_started = True
                try:
                    async for kind, value in self.brain.respond(self.memory, candidates or []):
                        if not self.is_current(epoch):
                            return
                        if output.done() and output.exception():
                            raise output.exception()
                        decision = getattr(self.brain, "last_decision", None) or {}
                        if not llm_first and decision:
                            if decision.get("path") == "rule":
                                # Lexical reuse never called the model; clear
                                # the provisional LLM span instead of logging
                                # rule-decision time as LLM latency.
                                if turn is not None:
                                    turn.llm_start = None
                                    turn.llm_first_token = None
                                llm_started = False
                            else:
                                if self.performance_monitor and turn:
                                    self.performance_monitor.mark_llm_first_token(
                                        bool(getattr(self.cloud, "last_llm_cache_hit", False)),
                                        turn)
                                llm_first = True
                        if kind in {"say", "reuse"}:
                            first_answer.set()
                            path = decision.get("path")
                            if not path:
                                path = "rule" if kind == "reuse" else (
                                    "regenerated" if candidates else "direct")
                            self.turn_metrics["reuse_path"] = path
                            if "decision_recorded" not in self.turn_metrics:
                                self.turn_metrics["decision_recorded"] = True
                                self.turn_metrics["decision_source"] = kind
                                latency_ms = round(
                                    (time.perf_counter() - self.turn_metrics["started"]) * 1000, 1)
                                candidate_id = decision.get("candidate_id")
                                if candidate_id is None and kind == "reuse":
                                    match = next((c for c in (candidates or [])
                                                  if c.reply.text == value.text), None)
                                    candidate_id = match.id if match is not None else "unknown"
                                if kind == "reuse":
                                    self.turn_metrics["reused"] = True
                                    self.turn_metrics["reuse_candidate_id"] = candidate_id or "unknown"
                                    if self.decision_tracker:
                                        self.decision_tracker.record_reuse(
                                            candidate_id or "unknown", latency_ms, path=path)
                                        if candidates:
                                            self.decision_tracker.record_prediction_hit(
                                                candidate_id or "unknown")
                                elif self.decision_tracker:
                                    self.decision_tracker.record_say(latency_ms, path=path)
                                    # "direct" means no forecast reached the
                                    # decision point (e.g. an unasked question
                                    # cleared the candidates), so it is not a
                                    # prediction miss.  Only "regenerated" is.
                                    if path == "regenerated":
                                        self.decision_tracker.record_prediction_miss()
                            if "first_text_ms" not in self.turn_metrics:
                                self.turn_metrics["first_text_ms"] = round(
                                    (time.perf_counter() - self.turn_metrics["started"]) * 1000)
                                await self.send({"type": "response_source", "epoch": epoch,
                                                 "source": "prediction" if kind == "reuse" else "direct"})
                            candidate_id = (decision.get("candidate_id")
                                            if kind == "reuse" else None)
                            if candidate_id is None and kind == "reuse":
                                match = next((c for c in (candidates or [])
                                              if c.reply.text == value.text), None)
                                candidate_id = match.id if match is not None else None
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
                finally:
                    if self.performance_monitor and turn:
                        if llm_first:
                            self.performance_monitor.mark_llm_end(turn)
                        elif llm_started:
                            turn.llm_start = None
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
            # Forecast the next user reply while this response is playing. The
            # reuse path's state update is still in flight, so the forecast
            # gets a private copy and merges that update before snapshotting;
            # playback is never blocked. Eligibility still requires complete
            # playback (see user_text).
            if self.predictions and self.memory.status == "active":
                base = copy.deepcopy(self.memory)
                self.forecast = self.spawn(self._forecast(
                    epoch, opening, pending_update, base, user, before,
                    [u.say.model_dump() for u in current_units]))
            await output
            if self.is_current(epoch):
                self.reply_completed = all(unit.delivered for unit in current_units)
                self.phase = "listening"
                self.log("reply_finished", **{k: v for k, v in self.turn_metrics.items() if k != "started"})
                self._finish_turn(completed=True, turn=turn)

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
                self._finish_turn(completed=False, error=type(exc).__name__, turn=turn)
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

    async def _forecast(self, epoch: int, opening: bool, pending_update: asyncio.Task | None,
                        base: Memory, user: str, before: set[str], pending_reply: list[dict]):
        """Snapshot the next-turn forecast after this turn's state update lands.

        A reused reply starts playing before the model produces the update for
        that same user turn. Waiting here (in the forecast task, never in the
        playback path) and merging the update into a private copy keeps the
        snapshot in order without delaying audio. Live memory is still only
        committed after playback, and the update is never applied twice.
        """
        merged = False
        if pending_update is not None:
            try:
                update = await pending_update
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log("prediction_update_error", error=type(exc).__name__)
            else:
                if not self.is_current(epoch):
                    return
                base.apply(update, user, epoch, before)
                merged = True
        if not self.is_current(epoch):
            return
        snapshot = copy.deepcopy(self.brain.snapshot(base))
        snapshot["opening"] = opening
        snapshot["pending_reply"] = pending_reply
        self.log("prediction_snapshot", state_update=merged,
                 questions=len(base.questions), facts=len(base.facts),
                 status=base.status, delivered=len(base.delivered))
        await self._predict(epoch, snapshot)

    async def _predict(self, epoch: int, snapshot: dict):
        started = time.perf_counter()
        try:
            ready = []
            tasks = []
            audio_tasks: dict[str, asyncio.Task] = {}

            async def prepare(item):
                text_ready_ms = round((time.perf_counter() - started) * 1000)
                audio_started = time.perf_counter()
                pcm = None
                if self.voice:
                    text = item.reply.text
                    created_audio = text not in audio_tasks
                    if created_audio:
                        audio_tasks[text] = asyncio.create_task(self.cloud.prefetch(text))
                    pcm = await asyncio.shield(audio_tasks[text])
                    if not pcm:
                        # Without prepared audio this is not a cache hit: the
                        # reply would fall back to a blocking TTS request.
                        self.log("prediction_audio_miss", candidate_id=item.id,
                                 text_ready_ms=text_ready_ms)
                        return
                    if created_audio and self.decision_tracker:
                        # Count each actually prefetched PCM buffer once, even
                        # when several candidates share the same text.
                        self.decision_tracker.record_prediction_audio(
                            prefetched_bytes=len(pcm))
                if self.is_current(epoch):
                    if pcm:
                        self.prediction_audio[item.reply.text] = pcm
                    ready.append(item)
                    self.candidates = list(ready)
                    self.prediction_epoch = epoch
                    await self.send({"type": "candidates", "epoch": epoch, "items": [
                        {"id": candidate.id, "text": candidate.reply.text} for candidate in ready]})
                    self.log("prediction_ready", count=len(ready), candidate_id=item.id,
                             text_ready_ms=text_ready_ms,
                             audio_prepare_ms=round((time.perf_counter() - audio_started) * 1000),
                             ready_ms=round((time.perf_counter() - started) * 1000),
                             audio_bytes=len(pcm) if pcm else 0,
                             candidates=[candidate.model_dump() for candidate in ready])

            try:
                if hasattr(self.brain, "predict_batches"):
                    async for batch in self.brain.predict_batches(snapshot):
                        if not self.is_current(epoch):
                            return
                        tasks.extend(asyncio.create_task(prepare(item)) for item in batch)
                else:
                    batch = await self.brain.predict(snapshot)
                    tasks.extend(asyncio.create_task(prepare(item)) for item in batch)
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
        self._finish_turn(completed=False)
        self.candidates.clear()
        self.prediction_audio.clear()
        self._clear_staged()
        self.bridge_audio = None
        self.log("closed")
