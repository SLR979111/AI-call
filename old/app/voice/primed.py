"""Warm immutable audio for a bounded set of common workflow prompts."""
from __future__ import annotations

from contextlib import aclosing
from dataclasses import replace

from ..cancel import CancelToken
from .types import mono_ms


class PrimedTts:
    def __init__(self, delegate):
        self.delegate = delegate
        self.sample_rate = delegate.sample_rate
        self.audio = {}

    async def prime(self, texts):
        for text in texts:
            stream = self.delegate.synthesize(text, sample_rate=self.sample_rate, cancel=CancelToken())
            async with aclosing(stream) as chunks:
                self.audio[text] = tuple([chunk async for chunk in chunks])

    async def synthesize(self, text, *, voice="default", sample_rate=24000, cancel):
        if voice == "default" and sample_rate == self.sample_rate and text in self.audio:
            for chunk in self.audio[text]:
                cancel.check()
                yield replace(chunk, ts_mono_ms=mono_ms())
            return
        stream = self.delegate.synthesize(text, voice=voice, sample_rate=sample_rate, cancel=cancel)
        async with aclosing(stream) as chunks:
            async for chunk in chunks:
                yield chunk
