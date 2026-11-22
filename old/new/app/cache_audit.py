"""Append-only cache audit used to rebuild the persistent cache after a loss.

The audit is written at the provider boundary, where the exact cache key
inputs and the value are both available. Session JSONL alone cannot do this:
it does not contain full prompts, the model parameters, or the raw PCM.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any


class CacheAudit:
    """Write cache material as JSONL plus PCM blobs next to it.

    Disabled when ``path`` is empty. Every write is best-effort: a full disk or
    a closed file must not break a live call.
    """

    def __init__(self, path: str | Path | None):
        self.path = Path(path).expanduser() if path else None
        self.pcm_dir = (self.path.parent / "cache-audit-pcm") if self.path else None
        self._lock = threading.Lock()
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.pcm_dir.mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def _append(self, record: dict[str, Any]):
        if not self.enabled:
            return
        record = {"at": time.time(), **record}
        try:
            line = json.dumps(record, ensure_ascii=False) + "\n"
            with self._lock:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
        except Exception:  # noqa: BLE001 - audit must not break a call
            pass

    def _write_pcm(self, pcm: bytes) -> str:
        if not self.enabled or self.pcm_dir is None:
            return ""
        digest = hashlib.sha256(pcm).hexdigest()
        target = self.pcm_dir / f"{digest}.pcm"
        if not target.exists():
            try:
                target.write_bytes(pcm)
            except Exception:  # noqa: BLE001
                return ""
        return str(target)

    def llm(self, *, messages: list[dict], model: str, temperature: float,
            max_tokens: int, response: str):
        self._append({
            "kind": "llm",
            "messages": messages,
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response": response,
        })

    def asr(self, *, pcm: bytes, text: str):
        path = self._write_pcm(pcm)
        self._append({
            "kind": "asr",
            "pcm_sha256": hashlib.sha256(pcm).hexdigest(),
            "pcm_file": path,
            "text": text,
        })

    def tts(self, *, text: str, voice: str, pcm: bytes):
        path = self._write_pcm(pcm)
        self._append({
            "kind": "tts",
            "text": text,
            "voice": voice,
            "pcm_file": path,
        })
