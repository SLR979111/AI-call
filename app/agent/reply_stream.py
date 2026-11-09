"""Incremental extraction of the natural-language `reply` field from JSON.

The LLM prompt asks for one JSON object. In `reply` stream mode we must not
send raw JSON tokens to TTS; this small parser waits only until the reply
string is complete, then returns its decoded natural-language value.
"""
from __future__ import annotations

import json


class ReplyExtractor:
    """Extract the top-level `"reply"` string from a streaming JSON buffer."""

    def __init__(self, max_buffer: int = 20000) -> None:
        self.buffer = ""
        self.done = False
        self.reply = ""
        self.max_buffer = max_buffer

    def feed(self, text: str) -> str | None:
        """Append a token; return the decoded reply once it is complete."""
        if self.done or not text:
            return None
        self.buffer += text
        self.buffer = self.buffer[-self.max_buffer:]

        key = self.buffer.find('"reply"')
        if key < 0:
            return None
        colon = self.buffer.find(":", key + 7)
        if colon < 0:
            return None
        quote = self.buffer.find('"', colon + 1)
        if quote < 0:
            return None

        # Scan the JSON string, respecting backslash escapes.
        escaped = False
        for index in range(quote + 1, len(self.buffer)):
            char = self.buffer[index]
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == '"':
                raw = self.buffer[quote + 1:index]
                try:
                    value = json.loads('"' + raw + '"')
                except (TypeError, ValueError):
                    return None
                self.reply = value
                self.done = True
                return value
        return None
