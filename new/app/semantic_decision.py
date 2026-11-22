"""Semantic tie-breaker for forecast reuse.

The lexical rules in :mod:`app.context_aware_decision` remain the fast path and
the only path that can answer in a couple of milliseconds. This module matters
on the *slow* path: the rules were about to reject every candidate and hand the
turn to the model, which costs one to three seconds. A sentence embedding can
still tell that "给远山的授权" answers an offer of "是远杉转授权，还是走经销商".

It never decides on its own:

* it only runs when the caller is already on the regeneration path;
* the assistant must have just asked a question and the user must have given a
  short answer (a new question is never rescued);
* the best candidate must clear an absolute score *and* a margin over the
  runner-up, otherwise the model still decides.

The embedder is optional. ``SEMANTIC_DECISION`` defaults to ``0`` but a missing
model directory, a missing ``sentence-transformers`` install or a load failure
all degrade to the lexical rules instead of failing a turn.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import threading
from pathlib import Path
from typing import Any, TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .models import Candidate


MODEL_NAME = "bge-small-zh-v1.5"
DEFAULT_THRESHOLD = 0.72
DEFAULT_MARGIN = 0.05
DEFAULT_TIMEOUT_MS = 120
MAX_ANSWER_CHARS = 40

# A question or a farewell is a new intent, not an answer to the last question.
_QUESTION = re.compile(r"[?？]|为什么|怎么|如何|哪里|哪儿|多少|几个|能不能|可不可以|是不是|有没有")
_FAREWELL = re.compile(r"再见|拜拜|挂了|先这样|不用了|算了")
# Embeddings encode topic, not polarity: "不是经销商" scores like "经销商".
_NEGATION = re.compile(r"不|没|未|别|并非")


def default_model_dir() -> str:
    """Locate the model wherever the process was started from."""
    explicit = os.environ.get("SEMANTIC_MODEL_DIR")
    if explicit:
        return explicit
    here = Path(__file__).resolve()
    for base in (Path.cwd(), here.parents[2], here.parents[1]):
        candidate = Path(base) / "models" / MODEL_NAME
        if candidate.is_dir():
            return str(candidate)
    return str(Path("models") / MODEL_NAME)


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / denominator) if denominator else 0.0


class SemanticMatcher:
    """Score short answers against forecast conditions with sentence embeddings."""

    def __init__(
        self,
        model_dir: str | None = None,
        *,
        embedder: Any = None,
        threshold: float | None = None,
        margin: float = DEFAULT_MARGIN,
        timeout_ms: float = DEFAULT_TIMEOUT_MS,
    ):
        self.model_dir = model_dir or default_model_dir()
        self.threshold = DEFAULT_THRESHOLD if threshold is None else threshold
        self.margin = margin
        self.timeout = max(0.0, timeout_ms) / 1000.0
        self._embedder = embedder
        self._embedder_ready = embedder is not None
        self._load_failed = False
        self._lock = threading.Lock()
        self._condition_cache: dict[tuple[str, str], np.ndarray] = {}

    # -- construction -----------------------------------------------------
    @classmethod
    def from_env(cls) -> "SemanticMatcher | None":
        """Build from the environment, or ``None`` when the feature is off."""
        if os.environ.get("SEMANTIC_DECISION", "0").strip() != "1":
            return None
        model_dir = default_model_dir()
        if not os.path.isdir(model_dir):
            print(f"⚠️  语义裁决未启用：找不到模型目录 {model_dir}，仍用词典规则")
            return None
        try:
            threshold = float(os.getenv("SEMANTIC_DECISION_THRESHOLD", DEFAULT_THRESHOLD))
            margin = float(os.getenv("SEMANTIC_DECISION_MARGIN", DEFAULT_MARGIN))
            timeout_ms = float(os.getenv("SEMANTIC_DECISION_TIMEOUT_MS", DEFAULT_TIMEOUT_MS))
        except ValueError:
            return None
        if not (0 <= threshold <= 1 and 0 <= margin <= 2 and math.isfinite(timeout_ms) and timeout_ms > 0):
            return None
        return cls(model_dir, threshold=threshold, margin=margin, timeout_ms=timeout_ms)

    @property
    def available(self) -> bool:
        return self._ensure_embedder() is not None

    def _ensure_embedder(self):
        if self._embedder_ready:
            return self._embedder
        if self._load_failed:
            return None
        with self._lock:
            if self._embedder_ready:
                return self._embedder
            if self._load_failed:
                return None
            try:
                from sentence_transformers import SentenceTransformer

                self._embedder = SentenceTransformer(self.model_dir, device="cpu", local_files_only=True)
                self._embedder_ready = True
            except Exception as error:  # noqa: BLE001 - degrade, never fail a turn
                self._load_failed = True
                self._embedder = None
                print(f"⚠️  语义裁决加载失败（{type(error).__name__}: {error}），仍用词典规则")
            return self._embedder

    # -- scoring ----------------------------------------------------------
    @staticmethod
    def _conditions(candidate: "Candidate") -> list[str]:
        conditions = [candidate.when, *candidate.examples]
        return [text for text in conditions if text and text.strip()]

    def _embed(self, texts: list[str]) -> np.ndarray | None:
        embedder = self._ensure_embedder()
        if embedder is None or not texts:
            return None
        vectors = embedder.encode(
            texts, normalize_embeddings=True, convert_to_numpy=True, batch_size=len(texts)
        )
        return np.asarray(vectors, dtype=np.float32)

    def _condition_vectors(self, candidate: "Candidate") -> np.ndarray | None:
        """All phrasings of one branch; the best match wins, not the average.

        A condition is written like a checklist and its examples like something
        the user would actually say, so averaging them dilutes both. Scoring
        each phrasing separately matches the way a person hears an answer.
        """
        conditions = self._conditions(candidate)
        if not conditions:
            return None
        key = (candidate.id, "\n".join(conditions))
        cached = self._condition_cache.get(key)
        if cached is not None:
            return cached
        vectors = self._embed(conditions)
        if vectors is None:
            return None
        with self._lock:
            if len(self._condition_cache) >= 512:
                self._condition_cache.pop(next(iter(self._condition_cache)))
            self._condition_cache[key] = vectors
        return vectors

    def eligible(self, user_input: str, assistant_last_text: str = "") -> bool:
        """Only an answer to a question the assistant just asked can be rescued."""
        text = (user_input or "").strip()
        if not text or len(text) > MAX_ANSWER_CHARS:
            return False
        if not (assistant_last_text or "").strip().rstrip().endswith(("？", "?")):
            return False
        if _QUESTION.search(text) or _FAREWELL.search(text) or _NEGATION.search(text):
            return False
        return True

    def rank(
        self, user_input: str, candidates: list["Candidate"], assistant_last_text: str = ""
    ) -> list[tuple["Candidate", float]]:
        """Return candidates sorted by similarity to the user's answer."""
        if not candidates or not self.eligible(user_input, assistant_last_text):
            return []
        vectors = self._embed([user_input.strip()])
        if vectors is None:
            return []
        user_vector = vectors[0]
        scored: list[tuple["Candidate", float]] = []
        for candidate in candidates:
            vectors_for_candidate = self._condition_vectors(candidate)
            if vectors_for_candidate is None:
                continue
            best = max(_cosine(user_vector, vector) for vector in vectors_for_candidate)
            scored.append((candidate, best))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored

    def best(
        self, user_input: str, candidates: list["Candidate"], assistant_last_text: str = ""
    ) -> "Candidate | None":
        """Synchronous decision used by tests and by :meth:`confirm`."""
        ranked = self.rank(user_input, candidates, assistant_last_text)
        if not ranked:
            return None
        top_candidate, top_score = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else -1.0
        if not math.isfinite(top_score) or not math.isfinite(runner_up) or top_score < self.threshold:
            return None
        if top_score - runner_up < self.margin:
            return None
        return top_candidate

    def warmup(self) -> bool:
        """Load the model off the turn path; False means lexical-only."""
        return self._ensure_embedder() is not None

    async def confirm(
        self, user_input: str, candidates: list["Candidate"], assistant_last_text: str = ""
    ) -> "Candidate | None":
        """Async wrapper: one bounded CPU hop, no exceptions escape."""
        if not candidates or not self.eligible(user_input, assistant_last_text):
            return None
        if not self._embedder_ready:
            return None
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self.best, user_input, candidates, assistant_last_text),
                timeout=max(self.timeout, 0.01),
            )
        except Exception as error:  # noqa: BLE001
            print(f"⚠️  语义裁决跳过（{type(error).__name__}）")
            return None


_SHARED: "SemanticMatcher | None | bool" = False


def shared_matcher() -> "SemanticMatcher | None":
    """One matcher per process: sessions share the loaded weights."""
    global _SHARED
    if _SHARED is False:
        _SHARED = SemanticMatcher.from_env()
    return _SHARED  # type: ignore[return-value]


def reset_shared() -> None:
    global _SHARED
    _SHARED = False


async def warmup_shared() -> bool:
    """Load the shared embedder without blocking the first turn."""
    matcher = shared_matcher()
    if matcher is None:
        return False
    try:
        return await asyncio.to_thread(matcher.warmup)
    except Exception as error:  # noqa: BLE001 - lexical rules still answer
        print(f"⚠️  语义裁决预热失败（{type(error).__name__}）")
        return False
