"""Cancellable conversation loop, with client-confirmed sentence delivery."""

from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from .voice_preview import PredictedLead, VoicePreview, restore_quotes, same_transcript, transcript_key
from .continuation import heard_question, hesitation, pickup
from .brain import Brain
from .models import Candidate, Memory, Say, TaskCard, ends_with_goodbye
from .providers import Cloud, ProviderError
from .playback import Playback, Unit, enqueue_for_playback
from .silent_logger import silent_log


PREDICTION_AUDIO_LIMIT_ENV = "PREDICTION_AUDIO_LIMIT"

# Longest filler the bridge may speak while the real answer is still being
# synthesized. At 24 kHz mono PCM16 this is one second, which covers the
# LLM-to-first-audio gap without talking over an answer that has arrived.
BRIDGE_MAX_BYTES = 48000
RESUME_DELAY = 4.0


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


PREDICTION_FORBIDDEN = (
    "保证通过", "包过", "百分百", "一定通过", "必过",
    "今天一定", "明天一定", "加急一定", "加急保证",
    "已帮您提交", "已转人工", "已为您登记",
    "京东商家资质审核客服", "京东官方审核人员",
)


def prediction_text_allowed(text: str) -> bool:
    """Fail closed for promises or an official identity in forecast speech."""
    return not any(term in text for term in PREDICTION_FORBIDDEN)


@dataclass
class AudioWarmup:
    task: asyncio.Task
    epoch: int
    candidate_id: str


class Session:
    def __init__(self, card: TaskCard, cloud: Cloud, brain: Brain,
                 send: Callable[[dict | bytes], Awaitable[None]], *, voice: bool,
                 predictions: bool = True, log_dir: Path | None = None,
                 decision_tracker=None, performance_monitor=None, on_hangup=None,
                 wait_for_input_end: Callable[[], Awaitable] | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.memory = Memory(card)
        self.cloud, self.brain, self.transport = cloud, brain, send
        self.voice, self.predictions = voice, predictions
        self.epoch = 0
        self.closed = False
        self.ended = False
        self.on_hangup = on_hangup
        self.phase = "ready"
        self.playback = Playback()
        self.jobs: set[asyncio.Task] = set()
        self.wait_for_input_end = wait_for_input_end
        self.resume_task: asyncio.Task | None = None
        self.resume_question = False
        self.closing_epoch = -1
        self.response: asyncio.Task | None = None
        self.forecast: asyncio.Task | None = None
        self.pending_update: asyncio.Task | None = None
        self.candidates: list[Candidate] = []
        self.prediction_audio: dict[str, bytes] = {}
        self.audio_warm: dict[str, AudioWarmup] = {}
        # Forecasts prepared for the turn a speech onset interrupted. They are
        # only candidates: the next user turn still has to pass the content
        # rules, and they expire as soon as another turn starts.
        self.staged_candidates: list[Candidate] = []
        self.staged_audio: dict[str, bytes] = {}
        self.staged_epoch = -1
        self.bridge_text = "嗯。"
        self.bridge_audio: bytes | None = None
        self.bridge_delay = 0.35
        self.filter_prediction_promises = os.getenv("PREDICTION_FILTER_PROMISES", "0") == "1"
        self.preview = None
        self.leads = []
        self.prediction_epoch = -1
        self.reply_completed = False
        self.starting = False
        self.send_lock = asyncio.Lock()
        self.turn_metrics: dict = {}
        self.turn = None  # LatencyBreakdown for the current turn
        self.log_path = None
        self.decision_tracker = decision_tracker
        self.performance_monitor = performance_monitor
        if log_dir:
            try:
                log_dir.mkdir(parents=True, exist_ok=True)
                self.log_path = log_dir / f"{self.id}.jsonl"
            except OSError:
                pass

    def log(self, event: str, **fields):
        # Write to session log file
        if self.log_path:
            try:
                with self.log_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"at": time.time(), "event": event, "epoch": self.epoch,
                                             **fields}, ensure_ascii=False) + "\n")
            except OSError:
                self.log_path = None
        
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

    async def send(self, payload: dict | bytes, *, epoch: int | None = None):
        if self.closed:
            return
        async with self.send_lock:
            if not self.closed and (epoch is None or epoch == self.epoch):
                await self.transport(payload)

    async def state(self):
        await self.send({"type": "state", "epoch": self.epoch, "phase": self.phase,
                         **self.memory.view()}, epoch=self.epoch)

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
                self.spawn(self.cloud.prefetch("好，再见。"))
            self.response = self.spawn(self._run(epoch, opening=True))

    def is_current(self, epoch: int):
        return not self.closed and not self.ended and epoch == self.epoch

    def prepare_lead(self, partial):
        key = transcript_key(partial)
        if self.ignores_hesitation(partial):
            return False
        if self.closed or self.ended or not self.predictions or len(self.leads) >= 2:
            return True
        if len(key) >= 5 and (not self.leads or len(key) >= len(self.leads[-1].prefix) + 5):
            lead = PredictedLead(partial, len(self.leads))
            self.leads.append(lead)
            snapshot = copy.deepcopy(self.brain.snapshot(self.memory))
            lead.task = self.spawn(lead.prepare(self.cloud, snapshot, partial))
        return len(self.leads) >= 2

    def voice_candidates(self, text):
        candidates = list(self.candidates) if self.reply_completed and self.prediction_epoch == self.epoch else []
        candidates.extend(lead.candidate for lead in self.leads if lead.matches(text))
        return candidates

    def discard_voice(self):
        self.take_preview()
        for lead in self.leads:
            lead.task.cancel()
        self.leads = []

    def ignores_hesitation(self, text: str) -> bool:
        return self.resume_task is not None and hesitation(text, answering_question=self.resume_question)

    def prepare_voice(self, text, origin="local"):
        if (not text or self.closed or self.ended or not self.voice
                or self.ignores_hesitation(text)):
            return
        state = self.brain.snapshot(self.memory)
        if self.preview and same_transcript(self.preview.text, text) and self.preview.state == state:
            return
        self.take_preview()
        memory = copy.deepcopy(self.memory)
        memory.history.append({"role": "user", "text": text})
        if memory.closing_reason(text):
            return
        candidates = self.voice_candidates(text)
        preview = VoicePreview(text, copy.deepcopy(state), origin=origin)
        preview.task = self.spawn(preview.prepare(self.brain, self.cloud, memory, candidates, list(self.leads)))
        self.preview = preview

    def take_preview(self, text=""):
        preview, self.preview = self.preview, None
        if preview:
            if (text and same_transcript(preview.text, text)
                    and preview.state == self.brain.snapshot(self.memory)):
                return preview
            preview.task.cancel()
        return None

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
        if source == "voice" and self.ignores_hesitation(text):
            self.discard_voice()
            return
        self.cancel_continuation()
        text = text[:2000]
        preview = self.take_preview(text if source == "voice" else "")

        usable = (self.reply_completed and self.prediction_epoch == self.epoch)
        candidates = self.voice_candidates(text) if source == "voice" else (list(self.candidates) if usable else [])
        audio = dict(self.prediction_audio) if usable else {}
        staged = None
        if usable:
            self._clear_staged()
        else:
            # A speech onset cleared the live slot; the forecasts it interrupted
            # may still answer this next user turn.
            staged = self._take_staged()
        self.prediction_audio.clear()
        leads = [lead for lead in self.leads if source == "voice" and lead.matches(text)]
        self.leads = [lead for lead in self.leads if lead not in leads]
        await self.interrupt("new_input")
        if self.forecast and not self.forecast.done():
            self.forecast.cancel()
        self.candidates = []
        self.prediction_audio.clear()
        self.prediction_epoch = -1
        self.epoch += 1
        epoch = self.epoch
        self.starting = True
        self.playback = Playback()
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
                if preview:
                    preview.task.cancel()
                    preview = None
                candidates = [confirmed]
                audio = ({confirmed.reply.text: staged_audio[confirmed.reply.text]}
                         if self.voice else {})
                staged_reuse = True
                self.log("predictions_reused_after_interrupt", candidate_id=confirmed.id)
            else:
                self.log("predictions_dropped_after_interrupt",
                         candidates=len(staged_candidates))
        self._begin_turn(source)
        self.turn_metrics.update(last_voice_client_ms=end_ms,
                                 candidates_available=len(candidates),
                                 staged_reuse=staged_reuse,
                                 draft_source=preview.origin if preview else None,
                                 draft_lead_ms=round((time.monotonic() - preview.started) * 1000) if preview else 0)
        self.log("user", text=text, source=source, staged_reuse=staged_reuse)
        await self.send({"type": "transcript", "text": text, "final": True, "epoch": self.epoch})
        if self.is_current(epoch):
            self.starting = False
            self.response = self.spawn(self._run(epoch, user=text, candidates=candidates, audio=audio, preview=preview, leads=leads))

    def cancel_continuation(self):
        if self.resume_task is not None:
            self.resume_task.cancel()
            self.resume_task = None

    async def _resume_after_pause(self, epoch: int, lines: list[Say], hangup: bool, user: str = ""):
        first = Unit(1, lines[0], {}) if lines else None
        preview = None
        # The bounded synthesis queue fills during the pause, then feeds the
        # standard output path. It is private until this turn wins the floor.
        if first is not None:
            render = self.spawn(self._synthesize(first))
        else:
            # Only a bridge was audible. Prepare the answer to the actual
            # question again, without adding another user turn to history.
            memory = copy.deepcopy(self.memory)
            while memory.history and memory.history[-1].get("bridge"):
                memory.history.pop()
            preview = VoicePreview(user, self.brain.snapshot(memory), origin="continuation")
            render = self.spawn(preview.prepare(self.brain, self.cloud, memory, []))
            preview.task = render
        try:
            await asyncio.sleep(RESUME_DELAY)
            if self.wait_for_input_end:
                await self.wait_for_input_end()
            if not self.is_current(epoch) or self.resume_task is not asyncio.current_task():
                return
            self.resume_task = None
            self.discard_voice()
            self._clear_staged()
            self.epoch += 1
            self.playback = Playback()
            self._begin_turn("continuation")
            self.response = asyncio.current_task()
            await self._run(self.epoch, continuation=lines, prepared=first, resume_hangup=hangup,
                            user=user, preview=preview)
        finally:
            render.cancel()
            await asyncio.gather(render, return_exceptions=True)
            if self.resume_task is asyncio.current_task():
                self.resume_task = None

    async def interrupt(self, reason: str, *, expected_epoch: int | None = None,
                        evidence: dict | None = None):
        if self.closed or self.ended:
            return
        if expected_epoch is not None and expected_epoch != self.epoch:
            return
        if reason in {"new_input", "text_input"}:
            self.cancel_continuation()
        self.discard_voice()
        if reason == "speech" and getattr(self.cloud, "is_local", False) and self.forecast:
            self.forecast.cancel()  # Reserve the local GPU for this caller's turn.
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
        self.playback.interrupt()
        if self.voice and self.phase == "speaking" and reason in {"speech", "manual"}:
            unfinished = [u for u in self.units.values() if not u.delivered and not u.entry.get("bridge")]
            if unfinished:
                first, *rest = unfinished
                heard = first.entry["text"]
                lines = [pickup(first.say, heard), *(u.say for u in rest)]
                self.resume_question = heard_question(first.say, heard) or bool(
                    not heard and self.memory.last_question)
                self.resume_task = self.spawn(self._resume_after_pause(
                    self.epoch, lines, self.closing_epoch == old_epoch))
                self.log("continuation_scheduled", interrupted_epoch=old_epoch, unit=first.number,
                         spoken=heard, remaining=first.entry["remaining_text"], pickup=lines[0].text)
            elif self.units and all(u.entry.get("bridge") for u in self.units.values()):
                user = next((e["text"] for e in reversed(self.memory.history) if e["role"] == "user"), "")
                if user:
                    self.resume_question = False
                    self.resume_task = self.spawn(self._resume_after_pause(self.epoch, [], False, user))
        self.candidates = []
        self.prediction_audio.clear()
        self.prediction_epoch = -1
        self.reply_completed = False
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

    @property
    def units(self) -> dict[int, Unit]:
        return self.playback.units

    async def playback_progress(self, epoch: int, unit_id: int, samples: int, *, done: bool = False):
        if self.is_current(epoch):
            completed = self.playback.acknowledge(unit_id, samples, done)
            if completed is not None:
                self._delivered(completed)
                await self.state()

    def _delivered(self, unit: Unit):
        if unit.delivered:
            return
        unit.delivered = True
        unit.entry["delivery"] = "complete"
        unit.entry.pop("played_ms", None)
        unit.say.covers = self.memory.delivered_covers(unit.say)
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
                         "input_source": self.turn_metrics.get("input_source", "opening")}, epoch=epoch)

    async def _synthesize(self, unit: Unit, pcm: bytes | None = None, *,
                          preview=None, turn=None):
        await unit.synthesize(self.cloud, pcm, preview, self.performance_monitor,
                              turn if turn is not None else self.turn)

    async def _output(self, epoch: int, queue: asyncio.Queue):
        playback = self.playback
        while self.is_current(epoch):
            unit = await queue.get()
            if not self.is_current(epoch):
                return
            if unit is None:
                if self.voice:
                    await playback.wait_until_delivered()
                return
            self.phase = "speaking"
            await self.state()
            if not self.voice:
                self._delivered(unit)
                continue
            async for packet in playback.packets(epoch, unit):
                if not self.is_current(epoch):
                    return
                await self.send(packet, epoch=epoch)
                if not self.is_current(epoch):
                    return
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
                             "unit": unit.number, "samples": unit.total}, epoch=epoch)
            # The player schedules the next unit behind this one. Overlap its
            # transfer with playback while keeping one shared 500 ms window.

    async def _run(self, epoch: int, *, opening: bool = False, user: str = "",
                   candidates: list[Candidate] | None = None,
                   audio: dict[str, bytes] | None = None, preview=None, leads=(),
                   continuation: list[Say] | None = None, prepared: Unit | None = None, resume_hangup=False):
        queue: asyncio.Queue = asyncio.Queue(maxsize=5)
        children: list[asyncio.Task] = [preview.task] if preview else []
        children.extend(lead.task for lead in leads)
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

        preview_audio_claimed = False

        async def say(line: Say, *, bridge_pcm: bytes | None = None, candidate_id: str | None = None):
            nonlocal preview_audio_claimed
            if not self.is_current(epoch):
                raise asyncio.CancelledError()
            entry = {"role": "assistant", "text": line.text, "delivery": "unplayed"}
            if bridge_pcm is not None:
                entry["bridge"] = True
            self.memory.history.append(entry)
            use_prepared = prepared is not None and not current_units
            unit = prepared if use_prepared else Unit(len(current_units) + 1, line, entry)
            unit.entry = entry
            current_units.append(unit)
            self.units[unit.number] = unit
            if self.voice and not use_prepared:
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
                prepared_preview = preview if preview and not preview_audio_claimed and preview.first_text == line.text else None
                if prepared_preview:
                    preview_audio_claimed = True
                children.append(self.spawn(self._synthesize(unit, pcm, preview=prepared_preview, turn=turn)))
            await self.send({"type": "utterance", "epoch": epoch, "unit": unit.number,
                             "text": line.text, "rate": 24000, "candidate_id": candidate_id,
                             "filler": bridge_pcm is not None}, epoch=epoch)
            await enqueue_for_playback(queue, unit, output)

        try:
            self.phase = "thinking"
            await self.send({"type": "reply_start", "epoch": epoch}, epoch=epoch)
            await self.state()
            if continuation:
                hangup = resume_hangup
                for line in continuation:
                    await say(line)
            elif opening:
                opening_text = self.memory.card.opening
                question = opening_text.split("。")[-1] if opening_text.endswith(("？", "?")) else ""
                await say(Say(text=opening_text, question=question))
            elif self.memory.closing_reason(user):
                if self.memory.status != "complete":
                    self.memory.status = "paused"
                hangup = True
                await say(Say(text="好，再见。"))
            else:
                if self.voice and self.bridge_audio and not (preview and preview.origin == "continuation"):
                    children.append(self.spawn(bridge()))
                if self.performance_monitor and turn:
                    self.performance_monitor.mark_llm_start(turn)
                    llm_started = True
                try:
                    def respond():
                        nonlocal preview
                        preview = None
                        return self.brain.respond(self.memory, candidates or [])

                    events = preview.events(respond) if preview else respond()
                    async for kind, value in events:
                        if not self.is_current(epoch):
                            return
                        if output.done() and output.exception():
                            raise output.exception()
                        decision = getattr(preview or self.brain, "last_decision", None) or {}
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
                                                 "source": "prediction" if kind == "reuse" else "direct"}, epoch=epoch)
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
                            if preview and preview.text != user:
                                value = restore_quotes(value, preview.text, user)
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
                if hangup and not ends_with_goodbye(last_reply.text):
                    await say(Say(text="再见。"))
            if hangup:
                self.closing_epoch = epoch
            await enqueue_for_playback(queue, None, output)
            # Forecast the next user reply while this response is playing. The
            # reuse path's state update is still in flight, so the forecast
            # gets a private copy and merges that update before snapshotting;
            # playback is never blocked. Eligibility still requires complete
            # playback (see user_text).
            if self.predictions and not hangup and self.memory.status == "active":
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
                                 "reused": self.turn_metrics.get("reused", False)}, epoch=epoch)
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
                    if self.memory.status == "active":
                        self.memory.status = "paused"
                    self.ended = True
                    self.phase = "ended"
                    self.candidates = []
                    self.prediction_epoch = -1
                    await self.state()
                    await self.send({"type": "hangup", "epoch": epoch, "reason": self.memory.status})
                    if self.on_hangup:
                        await self.on_hangup()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.is_current(epoch):
                self.playback.interrupt()
                # Track error in performance monitor
                if self.performance_monitor:
                    self.performance_monitor.record_error(type(exc).__name__)
                
                self.reply_completed = False
                self.candidates = []
                self.prediction_audio.clear()
                self.prediction_epoch = -1
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
            # Never put a long filler ahead of a ready answer. At 24 kHz mono
            # PCM16, 48,000 bytes is one second: a filler may cover the gap
            # until the answer starts (~0.95 s after the LLM's first token),
            # but must not talk over it. A synthesized "嗯。" measures ~0.76 s,
            # so the earlier 0.5 s ceiling rejected every real clip and left
            # the bridge permanently silent. Skip rather than truncate.
            if not self.closed and pcm and len(pcm) <= BRIDGE_MAX_BYTES:
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

    def _request_prediction_audio(self, epoch: int, item: Candidate):
        text = item.reply.text
        if text in self.prediction_audio:
            return
        request = self.audio_warm.get(text)
        if request is None:
            task = self.spawn(self._warm_prediction(text))
            self.audio_warm[text] = AudioWarmup(task, epoch, item.id)
        else:
            # A later forecast can claim an in-flight synthesis of the same line.
            request.epoch = epoch
            request.candidate_id = item.id

    async def _warm_prediction(self, text: str):
        started = time.perf_counter()
        request = self.audio_warm[text]
        try:
            warm = getattr(self.cloud, "warm", None)
            pcm = await (warm(text) if warm is not None else self.cloud.prefetch(text))
            warm_ms = round((time.perf_counter() - started) * 1000)
            if not pcm:
                self.log("prediction_audio_miss", candidate_id=request.candidate_id,
                         warm_ms=warm_ms)
                return
            if self.decision_tracker:
                self.decision_tracker.record_prediction_audio(prefetched_bytes=len(pcm))
            current = self.is_current(request.epoch)
            if current:
                self.prediction_audio[text] = pcm
            self.log("prediction_audio_ready", candidate_id=request.candidate_id,
                     audio_bytes=len(pcm), warm_ms=warm_ms, in_turn=current)
        except Exception as exc:
            self.log("prediction_audio_miss", candidate_id=request.candidate_id,
                     error=type(exc).__name__)
        finally:
            self.audio_warm.pop(text, None)

    async def _prediction_batches(self, snapshot):
        if hasattr(self.brain, "predict_batches"):
            async for batch in self.brain.predict_batches(snapshot):
                yield batch
        else:
            yield await self.brain.predict(snapshot)

    async def _predict(self, epoch: int, snapshot: dict):
        started = time.perf_counter()
        limit = prediction_audio_limit_from_env()
        if limit is not None:
            self.log("prediction_audio_limit", limit=limit)
        ready: list[Candidate] = []
        received = 0
        try:
            async for batch in self._prediction_batches(snapshot):
                for item in batch:
                    if not self.is_current(epoch):
                        return
                    received += 1
                    if self.filter_prediction_promises and not prediction_text_allowed(item.reply.text):
                        self.log("prediction_compliance_drop", candidate_id=item.id)
                        continue
                    text_ready_ms = round((time.perf_counter() - started) * 1000)
                    audio_status = "not_requested"
                    if self.voice:
                        if limit is None or received <= limit:
                            audio_status = "warming"
                            self._request_prediction_audio(epoch, item)
                        else:
                            audio_status = "limit_skipped"
                            self.log("prediction_audio_skipped", candidate_id=item.id,
                                     limit=limit, text_ready_ms=text_ready_ms)
                    ready.append(item)
                    self.candidates = ready.copy()
                    self.prediction_epoch = epoch
                    await self.send({"type": "candidates", "epoch": epoch, "items": [
                        {"id": candidate.id, "text": candidate.reply.text}
                        for candidate in ready]}, epoch=epoch)
                    self.log("prediction_ready", count=len(ready), candidate_id=item.id,
                             text_ready_ms=text_ready_ms,
                             ready_ms=round((time.perf_counter() - started) * 1000),
                             audio_status=audio_status,
                             candidates=[candidate.model_dump() for candidate in ready])
                if self.audio_warm:
                    # Start queued syntheses even when a batch and its transport
                    # both complete synchronously (e.g. cached simulations).
                    await asyncio.sleep(0)
        except Exception as exc:
            self.log("prediction_miss", error=type(exc).__name__)

    async def settle_audio(self) -> None:
        pending = [request.task for request in self.audio_warm.values()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.cancel_continuation()
        self.epoch += 1
        for job in list(self.jobs):
            job.cancel()
        if self.jobs:
            await asyncio.gather(*list(self.jobs), return_exceptions=True)
        self._finish_turn(completed=False)
        self.candidates.clear()
        self.prediction_audio.clear()
        self.audio_warm.clear()
        self._clear_staged()
        self.bridge_audio = None
        self.log("closed")
