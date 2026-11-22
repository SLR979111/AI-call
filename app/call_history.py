"""Read completed sessions recorded by the live call service."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path as RoutePath, Query, Request, Response

from .models import TaskCard

Mode = Literal["cloud", "local"]
Outcome = Literal["complete", "active", "paused", "declined", "unknown"]
OUTCOMES = ("complete", "active", "paused", "declined")


def task_key(card: TaskCard) -> str:
    raw = json.dumps(card.model_dump(), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class RecordedCall:
    id: str
    task: str
    title: str
    mode: Mode | None
    voice: bool
    outcome: str | None
    started_at: float | None
    ended_at: float
    version: int | None
    caller_turns: int
    preview: str
    transcript: list[dict]
    truncated: bool

    @property
    def incomplete(self) -> bool:
        return self.outcome in {"active", "paused", "declined"}

    def details(self) -> dict:
        return {"id": self.id, "voice": self.voice, "outcome": self.outcome,
                "version": self.version, "truncated": self.truncated}

    def summary(self) -> dict:
        return {
            "id": self.id, "title": self.title, "mode": self.mode, "voice": self.voice,
            "outcome": self.outcome, "started_at": self.started_at, "ended_at": self.ended_at,
            "version": self.version, "caller_turns": self.caller_turns, "preview": self.preview,
        }


def timestamp(value) -> float | None:
    return value if isinstance(value, (float, int)) and 0 <= value < 253402300800 and math.isfinite(value) else None


def read_call(path: Path, *, full=False) -> RecordedCall | None:
    start = end = None
    transcript = []
    caller_turns = 0
    first_caller = ""
    truncated = False

    def add(role, text, at):
        nonlocal caller_turns, first_caller, truncated
        if not isinstance(text, str) or not text.strip():
            return
        if role == "caller":
            caller_turns += 1
            if caller_turns == 1:
                first_caller = text
        truncated |= len(text) > 140
        transcript.append({"turn": len(transcript) + 1, "role": role,
                           "text": text if full else text[:140], "at": timestamp(at)})

    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            event = item.get("event")
            at = item.get("at")
            if event == "started":
                start = item
            elif event == "user":
                add("caller", item.get("text"), at)
            elif event == "delivered":
                add("assistant", item.get("text"), at)
            elif event in {"interrupted", "reply_error", "closed"}:
                for entry in item.get("cutoff") or []:
                    if isinstance(entry, dict):
                        add("cutoff", entry.get("text"), at)
                if event == "closed":
                    end = item
                    break
    if not (start and end):
        return None
    ended_at = timestamp(end.get("at"))
    if ended_at is None:
        return None
    card = TaskCard.model_validate(start["task"])
    mode = start.get("mode")
    outcome = end.get("status")
    preview = (first_caller or (transcript[0]["text"] if transcript else ""))[:140]
    truncated |= len(transcript) > 24
    if not full and len(transcript) > 24:
        transcript = transcript[:6] + transcript[-18:]
    return RecordedCall(
        id=path.stem, task=task_key(card), title=card.title, mode=mode if mode in ("local", "cloud") else None,
        voice=start.get("voice") is True, outcome=outcome if outcome in OUTCOMES else None,
        started_at=timestamp(start.get("at")), ended_at=ended_at, version=start.get("playbook_version"),
        caller_turns=caller_turns, preview=preview, transcript=transcript, truncated=not full and truncated,
    )


class CallArchive:
    def __init__(self, logs: Path):
        self.logs = logs
        self.scan_lock = Lock()
        self.index: dict[Path, tuple[tuple[int, int], RecordedCall | None]] = {}

    @staticmethod
    def read(path: Path, *, full=False):
        try:
            return read_call(path, full=full)
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def calls(self) -> list[RecordedCall]:
        # Only changed files are read again as the archive grows.
        with self.scan_lock:
            paths = set(self.logs.glob("*.jsonl"))
            for path in self.index.keys() - paths:
                del self.index[path]
            for path in paths:
                try:
                    stat = path.stat()
                except OSError:
                    self.index.pop(path, None)
                    continue
                stamp = (stat.st_mtime_ns, stat.st_size)
                if path not in self.index or self.index[path][0] != stamp:
                    self.index[path] = (stamp, self.read(path))
            return sorted((call for _, call in self.index.values() if call),
                          key=lambda call: (call.ended_at, call.id))

    def search(self, page: int, kind: str | None, outcome: Outcome | None, query: str) -> dict:
        query = query.strip().casefold()
        matches = [
            call for call in reversed(self.calls())
            if (kind is None or call.voice == (kind == "voice"))
            and (outcome is None or outcome == (call.outcome or "unknown"))
            and (not query or query in call.title.casefold() or query in call.id.casefold())
        ]
        size = 20
        page = min(page, max(1, (len(matches) + size - 1) // size))
        start = (page - 1) * size
        return {"calls": [call.summary() for call in matches[start:start + size]],
                "total": len(matches), "page": page, "page_size": size}


router = APIRouter(prefix="/api/call-history")


def archive(request: Request, response: Response) -> CallArchive:
    origin = request.headers.get("origin")
    host = request.headers.get("host")
    if (request.url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or request.headers.get("sec-fetch-site") == "cross-site"
            or (origin and origin not in {f"http://{host}", f"https://{host}"})):
        raise HTTPException(403, "请从通话页面查看历史。")
    response.headers["Cache-Control"] = "no-store"
    return request.app.state.call_archive


@router.get("")
async def list_calls(request: Request, response: Response,
                     page: Annotated[int, Query(ge=1)] = 1,
                     kind: Literal["voice", "text"] | None = None, outcome: Outcome | None = None,
                     q: Annotated[str, Query(max_length=100)] = ""):
    return await asyncio.to_thread(archive(request, response).search, page, kind, outcome, q)


@router.get("/{session_id}")
async def call_details(request: Request, response: Response,
                       session_id: Annotated[str, RoutePath(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]):
    calls = archive(request, response)
    call = await asyncio.to_thread(calls.read, calls.logs / f"{session_id}.jsonl", full=True)
    if call is None:
        raise HTTPException(404, "这通电话的记录不可用。", headers={"Cache-Control": "no-store"})
    return {**call.summary(), "transcript": call.transcript,
            "analysis_scope": {"mode": call.mode or "local"}}
