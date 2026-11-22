"""Overlap an ordinary response and its first TTS with final ASR verification."""

import asyncio
from copy import copy
import json
import re
import time
from contextlib import AsyncExitStack, aclosing

from pydantic import ValidationError

from .models import Candidate, Say
from .speech_start import spoken_prefix


# Only prepare unescaped spoken text from the first JSONL record. Unfamiliar
# field orders and escaped strings take the normal validated sentence path.
SAY_TEXT = re.compile(r'^\s*\{\s*"type"\s*:\s*"say"\s*,\s*"text"\s*:\s*"([^"\\\r\n]*)')


def first_clause(buffer):
    match = SAY_TEXT.match(buffer)
    if match is None:
        return None
    text = match[1]
    boundary = spoken_prefix(text)
    if boundary:
        text = boundary
    elif buffer[match.end():match.end() + 1] != '"':
        return None
    try:
        return Say(text=text).text
    except ValidationError:
        return None


def transcript_key(text):
    # Preserve decimals, number grouping, negation and internal question boundaries.
    return re.sub(r"(?<!\d)[，,](?!\d)|(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "",
                  text.strip().rstrip("。！？!?").rstrip("."))


def same_transcript(draft, final):
    # A pause after an isolated negation can reverse the meaning ("不，可以").
    if draft != final and re.search(r"(?:^|[，,])(?:不|没|别)[，,]", draft + "，" + final):
        return False
    if transcript_key(draft) != transcript_key(final):
        return False
    # A question distinguished only by intonation must use the punctuated final.
    return (final.rstrip().endswith(("？", "?")) == draft.rstrip().endswith(("？", "?"))
            or bool(re.search(r"吗|么|呢|哪|几|谁|何|多少|多久|能否|是否|[能行对好]不[能行对好]", draft)))


class PredictedLead:
    """Publish a single candidate as soon as its text is ready, while TTS runs."""

    def __init__(self, partial, number):
        self.id = f"live_lead_{number}"
        self.prefix = transcript_key(partial)
        self.candidate = None
        self.task = None

    async def prepare(self, cloud, snapshot, partial):
        context = {"task": snapshot["task"], "state": snapshot["state"], "recent": snapshot["dialogue"][-4:],
                   "partial": partial}
        messages = [{"role": "system", "content":
                     "根据电话任务、前文和未说完的一句话，预判对方最可能需要知道的具体事实或操作。"
                     "只输出一句约8至22字、可直接对对方说的中文答复。用任务中给出的名称、条件和已知事实。"
                     "不问候、不铺垫、不做外部承诺。把握不足就只输出短横线。"},
                    {"role": "user", "content": json.dumps(context, ensure_ascii=False)}]
        text = "".join([chunk async for chunk in cloud.chat(messages, max_tokens=72)]).strip()
        if not re.fullmatch(r"[\u3400-\u9fffA-Za-z0-9、，,：:%％.。！!?？（）() ]{5,44}", text):
            return None
        self.candidate = Candidate(id=self.id, when="仅为预合成首句，须按本轮完整原话核实",
                                   reply=Say(text=text))
        return await cloud.prefetch(text)

    def matches(self, text):
        return self.candidate is not None and transcript_key(text).startswith(self.prefix)


def restore_quotes(update, draft, final):
    # State updates still require literal evidence from the confirmed transcript.
    # Restore a comma or CJK word spacing removed by the first-pass recognizer.
    def quoted(text):
        if not text or text in final or text not in draft:
            return text
        pattern = r"[，,\s]*".join(re.escape(char) for char in text)
        match = re.search(pattern, final)
        if match and transcript_key(match[0]) == transcript_key(text):
            return match[0]
        return text

    facts = [fact.model_copy(update={"quote": quoted(fact.quote)}) for fact in update.facts]
    return update.model_copy(update={"facts": facts, "confirmation": quoted(update.confirmation),
                                     "disposition_quote": quoted(update.disposition_quote)})


class VoicePreview:
    def __init__(self, text, state, *, origin="local"):
        self.origin = origin
        self.started = time.monotonic()
        self.text = text
        self.state = state
        self.first_text = None
        self.last_decision = None
        self.events_queue = asyncio.Queue()
        self.audio_queue = asyncio.Queue()
        self.suffix_queue = None
        self.task = None

    async def prepare(self, brain, cloud, memory, candidates, leads=()):
        # A speculative reply has its own decision metrics until accepted.
        brain = copy(brain)
        self.local = getattr(cloud, "is_local", False)
        async def synthesize(text, target):
            try:
                lead = next((item for item in leads if item.candidate and text == item.candidate.reply.text), None)
                if lead:
                    try:
                        pcm = await asyncio.shield(lead.task)
                    except asyncio.CancelledError:
                        if asyncio.current_task().cancelling():
                            raise
                        pcm = None
                    except Exception:
                        pcm = None
                    if pcm:
                        for offset in range(0, len(pcm), 1920):
                            target.put_nowait(pcm[offset:offset + 1920])
                        return
                async for packet in cloud.speech(text):
                    target.put_nowait(packet)
            except Exception as exc:
                target.put_nowait(exc)
            finally:
                target.put_nowait(None)

        tasks = []
        prefix = None

        def prepare_clause(text):
            nonlocal prefix
            if prefix is None:
                prefix = text
                tasks.append(asyncio.create_task(synthesize(text, self.audio_queue)))

        try:
            async with aclosing(brain.respond(memory, candidates, on_first_clause=prepare_clause)) as events:
                async for kind, value in events:
                    if kind in {"say", "reuse"}:
                        memory.history.append({"role": "assistant", "text": value.text, "delivery": "unplayed"})
                        if self.first_text is None:
                            self.first_text = value.text
                            if prefix is not None and not value.text.startswith(prefix):
                                tasks[0].cancel()
                                self.audio_queue = asyncio.Queue()
                                prefix = None
                            if prefix is None:
                                prepare_clause(value.text)
                            suffix = value.text[len(prefix):]
                            if suffix:
                                self.suffix_queue = asyncio.Queue()
                                tasks.append(asyncio.create_task(synthesize(suffix, self.suffix_queue)))
                    self.events_queue.put_nowait((kind, value, getattr(brain, "last_decision", None)))
        except Exception as exc:
            self.events_queue.put_nowait(exc)
        finally:
            self.events_queue.put_nowait(None)
            if asyncio.current_task().cancelling():
                for task in tasks:
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def events(self, fallback):
        emitted = False
        while (event := await self.events_queue.get()) is not None:
            if isinstance(event, Exception):
                if emitted:
                    raise event
                async for item in fallback():
                    yield item
                return
            emitted = True
            kind, value, self.last_decision = event
            yield kind, value

    async def prepared_audio(self):
        for queue in (self.audio_queue, self.suffix_queue):
            if queue is not None:
                while (packet := await queue.get()) is not None:
                    if isinstance(packet, Exception):
                        raise packet
                    yield packet

    async def audio(self, fallback):
        # Only a confirmed, selected reply can hedge. The alternative uses
        # the same configured voice, and only one stream can reach the player.
        async with AsyncExitStack() as stack:
            prepared = await stack.enter_async_context(aclosing(self.prepared_audio()))
            first = asyncio.create_task(anext(prepared))
            pending = {first: prepared}
            tasks = [first]
            try:
                done, _ = await asyncio.wait(pending, timeout=None if self.local else .65)
                if not done or first.exception() is not None:
                    other = await stack.enter_async_context(aclosing(fallback()))
                    request = asyncio.create_task(anext(other))
                    pending[request] = other
                    tasks.append(request)
                while pending:
                    done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    for request in done:
                        stream = pending.pop(request)
                        try:
                            packet = request.result()
                        except Exception:
                            if not pending:
                                raise
                            continue
                        for loser in pending:
                            loser.cancel()
                        yield packet
                        async for packet in stream:
                            yield packet
                        return
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
