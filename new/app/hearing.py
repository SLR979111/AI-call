"""Continuous partial transcripts over a clip-based ASR, with separate endpointing.

One bounded request runs at a time. Genuine partial results may arrive while
speech continues; only the final endpoint can commit a user turn.
"""

from __future__ import annotations

import asyncio
import math
import re
import struct
from array import array
from collections import deque
from typing import Awaitable, Callable

from .providers import Cloud
from .vad import SpeechDetector


class Hearing:
    def __init__(self, cloud: Cloud, *, on_start: Callable[[], Awaitable],
                 on_text: Callable[[str, float], Awaitable],
                 on_partial: Callable[[str], Awaitable],
                 on_error: Callable[[str], Awaitable],
                 is_playing: Callable[[], bool] = lambda: False,
                 detector=None, first_pass=None, on_draft=None, discard_draft=None, on_lead=None, min_silence_ms=0):
        self.cloud = cloud
        self.first_pass, self.on_draft = first_pass, on_draft
        self.min_silence_ms = min_silence_ms
        self.lead_started = False
        self.on_lead = on_lead
        self.discard_draft = discard_draft
        self.local_final = None
        self.draft_job = None
        self.draft_at = -1
        self.on_start, self.on_text = on_start, on_text
        self.on_partial, self.on_error = on_partial, on_error
        self.is_playing = is_playing
        self.detector = detector or SpeechDetector()
        self.raw_detector = detector or SpeechDetector()
        self.last_onset: dict = {}
        self.preroll = deque(maxlen=30)
        self.onset_window = deque(maxlen=16)
        self.buffer = bytearray()
        self.active = False
        self.attack = 0
        self.quiet = 0
        self.floor = .001
        self.raw_floor = .003
        self.voiced_end = 0
        self.voice_tick = 0
        self.utterance_id = 0
        self.partial_at = -1
        self.partial_text = ""
        self.last_client_ms = 0.
        self.last_seq: int | None = None
        self.decode: asyncio.Task | None = None
        self.decode_at = -1
        self.finalizer: asyncio.Task | None = None
        self.jobs: set[asyncio.Task] = set()
        self.defer_to = 680
        self.blocked = False
        self.closed = False

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.jobs.add(task)
        def done(job):
            self.jobs.discard(job)
            if not job.cancelled():
                job.exception()  # ASR failures are surfaced by the finalizer.
        task.add_done_callback(done)
        return task

    async def feed(self, packet: bytes):
        if self.closed:
            return
        if len(packet) != 1316:
            await self.on_error("音频组件版本不一致，请刷新页面后重新开始通话。")
            return
        seq, client_ms = struct.unpack_from("<Id", packet)
        if not math.isfinite(client_ms):
            return
        flags, raw_rms, clean_rms, reference_rms, echo_score, raw_echo = struct.unpack_from("<Ifffff", packet, 12)
        if flags & ~7 or not all(math.isfinite(v) and 0 <= v <= 1.0001 for v in (raw_rms, clean_rms, reference_rms, echo_score, raw_echo)):
            return
        pcm = packet[36:676]
        raw_pcm = packet[676:]
        samples = array("h", pcm)
        rms = math.sqrt(sum(x*x for x in samples) / len(samples)) / 32768
        playback = self.is_playing() or bool(flags & 2)
        threshold = max(.006 if playback else .004, self.floor * 2.5)
        # Only speech on the echo-cancelled, noise-suppressed signal can open a
        # turn. Reference-tail frames retain the stricter interruption gate.
        probability = self.detector.feed(pcm)
        raw_probability = self.raw_detector.feed(raw_pcm)
        if not playback and raw_probability < .2:
            self.raw_floor=max(.0005,min(.05,self.raw_floor*.98+raw_rms*.02))
        # During initial echo-filter convergence, independent near-end speech
        # can be over-suppressed. Only a high-confidence, poorly echo-matching
        # raw signal can rescue it; this path closes after the filter warms.
        rescued = (bool(flags & 4) and playback and raw_probability >= .8
                   and raw_echo < .75 and raw_rms > max(.025,self.raw_floor*4))
        echo_dominated = playback and raw_echo > .82 and rms < raw_rms * .4
        voiced = rescued or (probability >= (.65 if playback else .6) and rms > threshold
                             and echo_score < .6 and not echo_dominated)
        if rescued and rms < raw_rms * .5:
            pcm = raw_pcm
        if self.last_seq is not None and seq != (self.last_seq + 1) % (2**32):
            self.reset()
            self.blocked = True
            await self.on_error("音频传输中断了，请停顿一下后重说。")
        self.last_seq = seq
        if self.blocked:
            self.quiet = 0 if voiced else self.quiet + 20
            if self.quiet >= 500:
                self.blocked = False
                self.quiet = 0
            return
        if not self.active:
            self.preroll.append(pcm)
            # Echo and partly suppressed speech must not raise the estimated
            # background floor and make subsequent user speech inaudible.
            if not playback and probability < .2:
                self.floor = max(.0002, min(.004, .98*self.floor + .02*rms))
            # Syllables contain brief gaps. Require 160 ms of independent
            # speech in a bounded 320 ms window instead of resetting on each
            # unvoiced consonant or momentary residual-echo veto.
            self.onset_window.append(voiced)
            self.attack = sum(self.onset_window) * 20
            if not voiced or self.attack < (160 if playback else 120):
                return
            self.active = True
            self.buffer = bytearray(b"".join(self.preroll))
            self.preroll.clear()
            self.quiet = 0
            self.voiced_end = len(self.buffer)
            self.voice_tick += 1
            self.utterance_id += 1
            self.last_client_ms = client_ms
            self.last_onset = {"rms": round(rms, 5), "threshold": round(threshold, 5),
                               "attack_ms": self.attack, "playback": playback,
                               "raw_rms": round(raw_rms, 5), "reference_rms": round(reference_rms, 5),
                               "speech_probability": round(probability, 4), "echo_score": round(echo_score, 4),
                               "raw_probability": round(raw_probability, 4), "raw_echo_score": round(raw_echo, 4),
                               "startup_rescue": rescued,
                               "detector": "aec3_ns_silero"}
            await self.on_start()
            return
        self.buffer.extend(pcm)
        if len(self.buffer) > 16000 * 2 * 30:
            self.reset()
            self.blocked = True
            await self.on_error("这一段超过30秒，请分成短一些的段落重说。")
            return
        if voiced:
            self.quiet = 0
            self.defer_to = 680
            self.voiced_end = len(self.buffer)
            self.voice_tick += 1
            self.last_client_ms = client_ms
            # A resumed utterance cancels endpoint commitment, not its earlier audio.
            if self.finalizer and not self.finalizer.done():
                self.finalizer.cancel()
            # Don't cancel every live request as new frames arrive: that starves
            # partial results during continuous speech. Sample ~700 ms apart.
            if (self.voiced_end >= 22400 and self.voiced_end - self.decode_at >= 22400
                    and (not self.decode or self.decode.done())):
                if self.on_lead and not self.lead_started:
                    self.request_first_pass(for_lead=True)
                self.request_decode()
        else:
            self.quiet += 20
            # Overlap recognition of the complete clip with the endpoint wait.
            # A slow partial for an older clip must not hold up the final ASR.
            if self.quiet >= 40 and self.draft_at != self.voiced_end:
                self.request_first_pass()
            if self.quiet >= 80 and self.decode_at != self.voiced_end:
                self.request_decode()
            if self.quiet >= max(self.min_silence_ms, self.defer_to) and (not self.finalizer or self.finalizer.done()):
                self.finalizer = self.spawn(self.finish(self.voice_tick, self.last_client_ms))

    def request_first_pass(self, *, for_lead=False):
        if not for_lead:
            self.draft_at = self.voiced_end
        if not self.first_pass or not self.on_draft:
            return
        audio = bytes(self.buffer[:self.voiced_end])
        utterance_id = self.utterance_id
        decoded_at = self.voiced_end

        async def first_pass():
            text = await self.first_pass.transcribe(audio)
            if (text and self.active and self.utterance_id == utterance_id
                    and self.partial_at < decoded_at and not self.closed):
                if for_lead:
                    self.lead_started = self.on_lead(text)
                else:
                    self.local_final = (decoded_at, text)
                    self.on_draft(text)

        if self.draft_job and not self.draft_job.done():
            self.draft_job.cancel()
        self.draft_job = self.spawn(first_pass())

    def request_decode(self):
        if self.decode and not self.decode.done():
            self.decode.cancel()
        self.decode_at = self.voiced_end
        audio = bytes(self.buffer[:self.voiced_end])
        utterance_id = self.utterance_id
        decoded_at = self.decode_at

        async def decode():
            text = await self.cloud.transcribe(audio)
            if (self.active and not self.closed and utterance_id == self.utterance_id
                    and decoded_at > self.partial_at):
                self.partial_at = decoded_at
                if decoded_at == self.voiced_end:
                    normalized = re.sub(r"[\s，。！？,.!?]", "", text)
                    if re.fullmatch(r"(?:是的|对的|好的|没错|明白了|知道了|可以|没有了|谢谢|再见)+", normalized):
                        self.defer_to = 360
                    elif text.strip().endswith(("？", "?")):
                        self.defer_to = 480
                # An earlier partial may already contain the complete wording.
                # The decode covering the final voiced frame can still start synthesis.
                local = getattr(self.cloud, "is_local", False)
                changed = text != self.partial_text
                if self.on_draft and decoded_at == self.voiced_end and text.strip() and (local or changed):
                    self.on_draft(text, origin="local" if local else "cloud")
                if text.strip() and changed:
                    self.partial_text = text
                    await self.on_partial(text)
            return text
        self.decode = self.spawn(decode())

    async def finish(self, tick: int, client_ms: float):
        if self.decode_at != self.voiced_end or self.decode is None:
            self.request_decode()
        try:
            local_final = self.local_final
            if local_final and local_final[0] == self.voiced_end:
                # A slow cloud call cannot hold a complete locally recognized
                # clip past the recognition budget. Endpoint checks below
                # still reject a resumed utterance or a trailing conjunction.
                remaining = max(0, (1050 - self.quiet) / 1000)
                try:
                    text = await asyncio.wait_for(asyncio.shield(self.decode), remaining)
                except Exception:
                    text = local_final[1]
            else:
                text = await asyncio.shield(self.decode)
            if not self.active or tick != self.voice_tick or self.closed:
                return
            if text.rstrip("，。！？,.!? ").endswith(("但是", "然后", "因为", "如果", "我想", "那个")) and self.quiet < 1150:
                self.defer_to = 1150
                return
            self.reset(discard_draft=not bool(text.strip()))
            if text.strip():
                await self.on_text(text, client_ms)
            else:
                await self.on_error("没有听清，请再说一次。")
        except asyncio.CancelledError:
            raise
        except Exception:
            if tick == self.voice_tick and not self.closed:
                self.reset()
                await self.on_error("这次语音识别未完成，请重说或使用文字输入。")

    def reset(self, *, discard_draft=True):
        if discard_draft and self.discard_draft:
            self.discard_draft()
        current = asyncio.current_task()
        for job in (self.decode, self.finalizer, self.draft_job):
            if job and job is not current and not job.done():
                job.cancel()
        self.decode = self.finalizer = None
        self.draft_at = self.decode_at = -1
        self.local_final = None
        self.lead_started = False
        self.active = False
        self.buffer.clear()
        self.preroll.clear()
        self.onset_window.clear()
        self.attack = self.quiet = self.voiced_end = 0
        self.defer_to = 680
        self.voice_tick += 1
        self.utterance_id += 1
        self.partial_at = -1
        self.partial_text = ""

    async def close(self):
        self.closed = True
        for job in list(self.jobs):
            job.cancel()
        if self.jobs:
            await asyncio.gather(*list(self.jobs), return_exceptions=True)
