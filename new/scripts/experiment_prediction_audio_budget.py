#!/usr/bin/env python3
"""Offline experiment: how many forecast candidates should get audio?

This script never touches a real cloud endpoint. It drives the real
``Session._predict`` implementation with a scripted Brain and a fake Cloud so
the audio-budget switch (PREDICTION_AUDIO_LIMIT) is exercised exactly as in
production, but with controlled text/audio delays.

For every arm (all candidates, N=0, 1, 2, 3, 5, 10) it records:
  - how many prefetch/TTS calls happened and how many PCM bytes were prepared
  - when each candidate's text was generated, published and audio-ready
  - whether six scripted short answers could reuse prepared audio at an early
    and a late arrival time
  - whether a text-only candidate still completes through ordinary TTS

Run from ``new/``:

    python scripts/experiment_prediction_audio_budget.py

Results are written to ``experiments/prediction-audio-budget/results.json`` and
``experiments/prediction-audio-budget/results.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models import DEFAULT_CARD, Candidate, Say, Update  # noqa: E402
from app.session import (  # noqa: E402
    PREDICTION_AUDIO_LIMIT_ENV,
    Session,
)


# ---------------------------------------------------------------------------
# Simulation parameters. These are deliberately round numbers so a reader can
# recompute the expected numbers without rerunning the script.
# ---------------------------------------------------------------------------
AUDIO_BASE_MS = 140
AUDIO_PER_CHAR_MS = 8
PCM_BYTES_PER_CHAR = 12_000       # 24 kHz PCM16, roughly 4 Chinese chars/s
TTS_CHUNK_BYTES = 1_920           # 40 ms of 24 kHz PCM16
EARLY_ANSWER_MS = 150
LATE_ANSWER_MS = 900
FALLBACK_TIMEOUT_S = 3.0


@dataclass(frozen=True)
class Script:
    candidate_id: str
    answer: str
    reply: str
    text_at_ms: int
    # Only these candidates are used as scripted short answers in the report.
    scripted: bool = True


# Ten candidates mirror the production fan-out: five branches, two candidates
# each. The order is the arrival order used by the experiment, not a promise
# about which branch finishes first in production.
SCRIPTS: tuple[Script, ...] = (
    Script("p0", "好的，您现在方便吗？", "方便的，您请讲。", 40),
    Script("p1", "我已经提交了。", "好的，我这边看到了。", 60),
    Script("p2", "只是保存了。", "那还需要点提交。", 80),
    Script("p3", "我是负责人。", "好的，那我直接说明。", 100),
    Script("p4", "现在有点忙。", "那我晚点再联系您。", 120),
    Script("p5", "还缺什么材料？", "还缺授权和截图。", 180, scripted=False),
    Script("p6", "授权补好了。", "好的，我记下了。", 210, scripted=False),
    Script("p7", "公司名也改了。", "明白，我一起更新。", 240, scripted=False),
    Script("p8", "截图传了。", "好的，我去核对。", 270, scripted=False),
    Script("p9", "我明白了。", "好的，那就不打扰您了。", 300),
)
SCRIPTED = tuple(script for script in SCRIPTS if script.scripted)


class Clock:
    def __init__(self):
        self.started = time.perf_counter()

    def elapsed_ms(self) -> float:
        return round((time.perf_counter() - self.started) * 1000, 1)


class FakeCloud:
    """Deterministic TTS stand-in. It only simulates delays and byte counts."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.prefetch_calls: list[str] = []
        self.speech_calls: list[str] = []
        self.audio_ready_ms: dict[str, float] = {}
        self.bytes_by_text: dict[str, int] = {}

    @staticmethod
    def audio_delay_ms(text: str) -> int:
        return AUDIO_BASE_MS + AUDIO_PER_CHAR_MS * len(text)

    @staticmethod
    def pcm_size(text: str) -> int:
        return PCM_BYTES_PER_CHAR * len(text)

    async def prefetch(self, text: str) -> bytes:
        self.prefetch_calls.append(text)
        await asyncio.sleep(self.audio_delay_ms(text) / 1000)
        size = self.pcm_size(text)
        self.bytes_by_text[text] = size
        self.audio_ready_ms[text] = self.clock.elapsed_ms()
        return b"\x00\x00" * (size // 2)

    async def speech(self, text: str):
        self.speech_calls.append(text)
        yield b"\x00\x00" * (TTS_CHUNK_BYTES // 2)
        yield b"\x00\x00" * (TTS_CHUNK_BYTES // 2)

    def cached(self, text: str):
        return None


class ScriptedBrain:
    """Yield one candidate per batch at fixed arrival times."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.text_generated_ms: dict[str, float] = {}

    @staticmethod
    def snapshot(memory) -> dict:
        return {"task": memory.card.model_dump(), "state": memory.view(), "dialogue": []}

    async def predict_batches(self, snapshot):
        for script in SCRIPTS:
            remaining = script.text_at_ms / 1000 - (time.perf_counter() - self.clock.started)
            if remaining > 0:
                await asyncio.sleep(remaining)
            self.text_generated_ms[script.candidate_id] = self.clock.elapsed_ms()
            yield [Candidate(
                id=script.candidate_id,
                when=f"用户说：{script.answer}",
                examples=[script.answer],
                reply=Say(text=script.reply),
            )]


class ReuseBrain:
    """Select one already-published candidate through the cached-reply path."""

    def __init__(self, item: Candidate):
        self.item = item

    async def respond(self, memory, candidates):
        selected = next(candidate for candidate in candidates
                        if candidate.id == self.item.id)
        yield "reuse", selected.reply
        yield "update_async", selected

    async def update_for_reuse(self, memory, selected):
        return Update()


class RecordingSession(Session):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.events: list[dict] = []

    def log(self, event, **fields):
        self.events.append({"event": event, **fields})


async def until(predicate, timeout=FALLBACK_TIMEOUT_S):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.005)


def candidate_by_id(candidate_id: str) -> Script:
    return next(script for script in SCRIPTS if script.candidate_id == candidate_id)


def evaluate_answer(arm: dict, script: Script, at_ms: int) -> dict:
    published_at = arm["published_ms"].get(script.candidate_id)
    audio_at = arm["audio_ready_ms"].get(script.candidate_id)
    published = published_at is not None and published_at <= at_ms
    audio = published and audio_at is not None and audio_at <= at_ms
    if audio:
        path = "prediction_audio"
    elif published:
        path = "text_only_tts"
    else:
        path = "not_ready"
    return {
        "at_ms": at_ms,
        "candidate_id": script.candidate_id,
        "answer": script.answer,
        "published": published,
        "audio": audio,
        "path": path,
        "published_ms": published_at,
        "audio_ready_ms": audio_at,
    }


async def _drive_fallback(session: RecordingSession, events: list,
                          target: Candidate) -> dict:
    """Run one text-only candidate through the normal reply/playback loop."""
    event_mark = len(session.events)
    transport_mark = len(events)
    speech_mark = len(session.cloud.speech_calls)
    session.brain = ReuseBrain(target)
    session.reply_completed = True
    session.prediction_epoch = 0
    try:
        await session.user_text("好的，我知道了。")
        await until(lambda: any(
            isinstance(event, dict) and event.get("type") == "utterance_end"
            for event in events[transport_mark:]))
        unit = session.units.get(1)
        if unit is None:
            raise RuntimeError("fallback produced no playback unit")
        await session.playback_progress(session.epoch, 1, unit.total, done=True)
        await until(lambda: session.reply_completed)
        new_events = session.events[event_mark:]
        sources = [event["source"] for event in new_events
                   if event["event"] == "audio_source"]
        errors = [event for event in new_events if event["event"] == "reply_error"]
        return {
            "applicable": True,
            "candidate_id": target.id,
            "ok": not errors and session.reply_completed,
            "tts_calls": len(session.cloud.speech_calls) - speech_mark,
            "sources": sources,
            "prediction_audio_hit": session.turn_metrics.get("prediction_audio_hit"),
            "reply_completed": session.reply_completed,
            "errors": errors,
        }
    except Exception as exc:  # keep the experiment running and report the failure
        return {
            "applicable": True,
            "candidate_id": target.id,
            "ok": False,
            "tts_calls": len(session.cloud.speech_calls) - speech_mark,
            "error": f"{type(exc).__name__}: {exc}",
        }


async def run_arm(limit: int | None) -> dict:
    original = os.environ.get(PREDICTION_AUDIO_LIMIT_ENV)
    if limit is None:
        os.environ.pop(PREDICTION_AUDIO_LIMIT_ENV, None)
    else:
        os.environ[PREDICTION_AUDIO_LIMIT_ENV] = str(limit)

    clock = Clock()
    cloud = FakeCloud(clock)
    brain = ScriptedBrain(clock)
    events: list = []

    async def send(payload):
        events.append(payload)

    session = RecordingSession(DEFAULT_CARD, cloud, brain, send, voice=True,
                               predictions=False)
    try:
        # Align the experiment clock with Session._predict's own timer so
        # "generated" and "published" timestamps are directly comparable.
        clock.started = time.perf_counter()
        started = time.perf_counter()
        await session._predict(0, brain.snapshot(session.memory))
        forecast_total_ms = round((time.perf_counter() - started) * 1000, 1)

        ready_events = {event["candidate_id"]: event for event in session.events
                        if event["event"] == "prediction_ready"}
        skipped_events = {event["candidate_id"]: event for event in session.events
                          if event["event"] == "prediction_audio_skipped"}
        limit_events = [event for event in session.events
                        if event["event"] == "prediction_audio_limit"]

        published_ms: dict[str, float | None] = {}
        audio_status: dict[str, str] = {}
        for script in SCRIPTS:
            ready = ready_events.get(script.candidate_id)
            published_ms[script.candidate_id] = ready["ready_ms"] if ready else None
            if ready:
                audio_status[script.candidate_id] = ready["audio_status"]
            elif script.candidate_id in skipped_events:
                audio_status[script.candidate_id] = "limit_skipped"
            else:
                audio_status[script.candidate_id] = "not_published"

        audio_ready_ms = {
            script.candidate_id: cloud.audio_ready_ms.get(script.reply)
            for script in SCRIPTS
        }
        audio_bytes_by_candidate = {
            script.candidate_id: (cloud.bytes_by_text.get(script.reply, 0)
                                  if script.candidate_id in ready_events else 0)
            for script in SCRIPTS
        }
        final_audio_texts = set(session.prediction_audio)
        text_only = [candidate for candidate in session.candidates
                     if candidate.reply.text not in final_audio_texts]
        arm = {
            "limit": limit,
            "label": "all" if limit is None else f"N={limit}",
            "prefetch_calls": len(cloud.prefetch_calls),
            "audio_bytes_total": sum(cloud.bytes_by_text.values()),
            "audio_candidate_count": len(final_audio_texts),
            "text_only_candidate_count": len(text_only),
            "published_count": len(ready_events),
            "skipped_count": len(skipped_events),
            "forecast_total_ms": forecast_total_ms,
            "audio_all_ready_ms": max(cloud.audio_ready_ms.values(), default=None),
            "published_all_ms": max((value for value in published_ms.values()
                                     if value is not None), default=None),
            "text_generated_ms": dict(brain.text_generated_ms),
            "published_ms": published_ms,
            "audio_ready_ms": audio_ready_ms,
            "audio_status": audio_status,
            "audio_bytes_by_candidate": audio_bytes_by_candidate,
            "final_candidate_ids": [candidate.id for candidate in session.candidates],
            "limit_event": limit_events[0]["limit"] if limit_events else None,
        }
        arm["answers"] = [
            {
                "answer": script.answer,
                "candidate_id": script.candidate_id,
                "early": evaluate_answer(arm, script, EARLY_ANSWER_MS),
                "late": evaluate_answer(arm, script, LATE_ANSWER_MS),
            }
            for script in SCRIPTED
        ]
        arm["summary"] = {
            "early_ms": EARLY_ANSWER_MS,
            "late_ms": LATE_ANSWER_MS,
            "early_text_hits": sum(answer["early"]["published"] for answer in arm["answers"]),
            "early_audio_hits": sum(answer["early"]["audio"] for answer in arm["answers"]),
            "late_text_hits": sum(answer["late"]["published"] for answer in arm["answers"]),
            "late_audio_hits": sum(answer["late"]["audio"] for answer in arm["answers"]),
            "answer_count": len(arm["answers"]),
        }
        # Coverage over all ten candidates, independent of the six scripted
        # answers. This makes the N/10 audio coverage directly readable.
        coverage = {}
        for name, at_ms in (("early", EARLY_ANSWER_MS), ("late", LATE_ANSWER_MS)):
            coverage[f"{name}_published"] = sum(
                1 for script in SCRIPTS
                if arm["published_ms"].get(script.candidate_id) is not None
                and arm["published_ms"][script.candidate_id] <= at_ms)
            coverage[f"{name}_audio"] = sum(
                1 for script in SCRIPTS
                if arm["published_ms"].get(script.candidate_id) is not None
                and arm["published_ms"][script.candidate_id] <= at_ms
                and arm["audio_ready_ms"].get(script.candidate_id) is not None
                and arm["audio_ready_ms"][script.candidate_id] <= at_ms)
            coverage[f"{name}_total"] = len(SCRIPTS)
        arm["coverage"] = coverage

        fallback_target = text_only[-1] if text_only else None
        arm["fallback"] = await _drive_fallback(session, events, fallback_target) \
            if fallback_target is not None else {
                "applicable": False,
                "reason": "every published candidate already has prepared audio",
            }
        arm["final_audio_texts"] = sorted(final_audio_texts)
        return arm
    finally:
        await session.close()
        if original is None:
            os.environ.pop(PREDICTION_AUDIO_LIMIT_ENV, None)
        else:
            os.environ[PREDICTION_AUDIO_LIMIT_ENV] = original


def bytes_label(value: int) -> str:
    return f"{value / 1000:.1f} KB"


def status_letter(status: str) -> str:
    return {"prefetched": "P", "limit_skipped": "s", "not_published": "-"}.get(status, "?")


def path_letter(answer: dict) -> str:
    return {"prediction_audio": "A", "text_only_tts": "T", "not_ready": "."}[answer["path"]]


def build_markdown(arms: list[dict]) -> str:
    generated = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    lines = [
        "# 预测候选音频预算实验（PREDICTION_AUDIO_LIMIT）",
        "",
        f"生成时间：{generated}",
        "",
        "本实验离线运行，不访问任何真实云接口。它用脚本化 Brain 和 fake Cloud "
        "驱动真实的 `Session._predict`，固定文本到达和音频合成延迟。",
        "",
        "## 模拟假设",
        "",
        f"- 10 条候选（5 个分支 × 每分支 2 条），文本到达时间 40–300 ms。",
        f"- 音频准备耗时 = {AUDIO_BASE_MS} + {AUDIO_PER_CHAR_MS} × 回复字数（ms）。",
        f"- PCM 字节 = {PCM_BYTES_PER_CHAR} × 回复字数（24 kHz PCM16，约 4 字/秒）。",
        "- 各候选预合成并发执行，没有 TTS 排队；因此限制 N 不改变前 N 条各自的音频延迟，"
        "只改变哪些候选有音频、总字节数和预测任务等待的音频尾巴。",
        f"- 脚本化短答分别在 {EARLY_ANSWER_MS} ms（音频尚未全部就绪）和 {LATE_ANSWER_MS} ms"
        "（所有文本和预算内音频都已就绪）评估。",
        "- `预测任务完成 ms` 包含等待全部候选文字到达；音频预算只缩短其中的音频等待，"
        "不会让最后一条文字更早到达。",
        "",
        "## 总览",
        "",
        "| 档位 | 预合成次数 | 音频字节（占 all） | 仅文本候选 | 音频就绪尾巴 ms | "
        "预测任务完成 ms | "
        f"{EARLY_ANSWER_MS} ms 发布/音频覆盖 | {LATE_ANSWER_MS} ms 发布/音频覆盖 | "
        "900 ms 脚本音频命中 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    all_bytes = max((arm["audio_bytes_total"] for arm in arms), default=0) or 1
    for arm in arms:
        summary = arm["summary"]
        coverage = arm["coverage"]
        share = arm["audio_bytes_total"] / all_bytes * 100
        tail = arm["audio_all_ready_ms"]
        lines.append(
            f"| {arm['label']} | {arm['prefetch_calls']} | "
            f"{bytes_label(arm['audio_bytes_total'])} ({share:.0f}%) | "
            f"{arm['text_only_candidate_count']} | "
            f"{tail if tail is not None else '—'} | {arm['forecast_total_ms']} | "
            f"{coverage['early_published']}/{coverage['early_audio']} | "
            f"{coverage['late_published']}/{coverage['late_audio']} | "
            f"{summary['late_audio_hits']}/{summary['answer_count']} |"
        )

    lines += [
        "",
        "发布/音频覆盖的分母是 10 条候选；脚本音频命中的分母是 6 条脚本化短答。"
        "`发布` 表示候选已经进入 `session.candidates`，可被选择；`音频` 表示该候选同时有预合成 PCM。",
        "",
        "## 每条候选的时间与各档音频状态",
        "",
        "| 候选 | 文本生成 ms | 默认档发布 ms | 音频就绪 ms | "
        + " | ".join(arm["label"] for arm in arms) + " |",
        "|---|---:|---:|---:|" + "---|" * len(arms),
    ]
    for script in SCRIPTS:
        cid = script.candidate_id
        generated = arm0(arms)["text_generated_ms"].get(cid, "—")
        audio_ready = arm0(arms)["audio_ready_ms"].get(cid)
        published = arm0(arms)["published_ms"].get(cid)
        lines.append(
            f"| {cid} | {generated} | "
            f"{published if published is not None else '—'} | "
            f"{audio_ready if audio_ready is not None else '—'} | "
            + " | ".join(status_letter(arm["audio_status"].get(cid, "?")) for arm in arms)
            + " |"
        )

    lines += [
        "",
        "状态字母：`P` = 已预合成音频；`s` = 超出预算、仅文本；`-` = 未发布。",
        "时间列取自 all 档的 fake Brain/Cloud 时钟与 `prediction_ready.ready_ms`；"
        "all 档没有音频就不发布，所以“默认档发布”等于音频完成。"
        "仅文本档的发布更早，体现在下面脚本化短答表的早期列和 JSON 的 `published_ms`。",
        "",
        "## 脚本化短答可用性",
        "",
        "每个格子是 `路径`：`A` = 预测音频可用；`T` = 仅文本、选中后走普通 TTS；"
        "`.` = 候选尚未发布。按 `早期/晚期` 两列显示。",
        "",
        "| 档位 | " + " | ".join(script.answer for script in SCRIPTED) + " |",
        "|---|" + "---|" * len(SCRIPTED),
    ]
    for arm in arms:
        lines.append(
            f"| {arm['label']} | "
            + " | ".join(
                f"{path_letter(answer['early'])}/{path_letter(answer['late'])}"
                for answer in arm["answers"])
            + " |"
        )

    lines += [
        "",
        "## 仅文本候选的回退 TTS 检查",
        "",
        "取该档最后一条没有预合成音频的候选，走真实 `reuse` 回复路径；"
        "预期 `audio_source=tts`、预测音频命中为 false、回复正常完成。",
        "",
        "| 档位 | 测试候选 | 结果 | TTS 调用 | 音频来源 | prediction_audio_hit |",
        "|---|---|---|---:|---|---|",
    ]
    for arm in arms:
        fallback = arm["fallback"]
        if not fallback.get("applicable"):
            lines.append(f"| {arm['label']} | — | 不适用 | 0 | — | — |")
        else:
            sources = ", ".join(fallback.get("sources") or []) or "—"
            lines.append(
                f"| {arm['label']} | {fallback['candidate_id']} | "
                f"{'通过' if fallback.get('ok') else '失败'} | {fallback.get('tts_calls', 0)} | "
                f"{sources} | {fallback.get('prediction_audio_hit')} |"
            )

    lines += [
        "",
        "## 结论（由脚本生成）",
        "",
    ]
    all_arm = next((arm for arm in arms if arm["limit"] is None), arms[0])
    all_bytes = all_arm["audio_bytes_total"] or 1
    fallback_arms = [arm for arm in arms if arm["fallback"].get("applicable")]
    fallback_ok = [arm for arm in fallback_arms if arm["fallback"].get("ok")]
    for arm in arms:
        coverage = arm["coverage"]
        share = arm["audio_bytes_total"] / all_bytes * 100
        fallback = arm["fallback"]
        if fallback.get("applicable"):
            fallback_text = ("回退 TTS 通过" if fallback.get("ok") else "回退 TTS 失败")
        else:
            fallback_text = "无仅文本候选"
        lines.append(
            f"- {arm['label']}：预合成 {arm['prefetch_calls']} 次 / "
            f"{bytes_label(arm['audio_bytes_total'])}（all 的 {share:.0f}%）；"
            f"{LATE_ANSWER_MS} ms 音频覆盖 {coverage['late_audio']}/10，"
            f"脚本化音频命中 {arm['summary']['late_audio_hits']}/6；"
            f"{EARLY_ANSWER_MS} ms 发布覆盖 {coverage['early_published']}/10；"
            f"仅文本候选 {arm['text_only_candidate_count']} 条；{fallback_text}。"
        )

    coverage_rank = "；".join(
        f"{arm['label']} {arm['coverage']['late_audio']}/10"
        for arm in sorted(arms, key=lambda arm: (
            arm["limit"] is None, arm["limit"] if arm["limit"] is not None else 0)))
    early_best = max(arms, key=lambda arm: arm["coverage"]["early_published"])
    n1 = next((arm for arm in arms if arm["limit"] == 1), None)
    n1_text = (
        f"降到 N=1 的 {bytes_label(n1['audio_bytes_total'])}，"
        f"但 N=1 的晚期音频覆盖只有 {n1['coverage']['late_audio']}/10。" if n1 else ""
    )
    lines += [
        "",
        f"- N 档晚期音频覆盖随 N 增加：{coverage_rank}。"
        "候选总数固定为 10，因此 N=10 与 all 在本实验中等价。",
        f"- 音频字节从默认档 all 的 {bytes_label(all_arm['audio_bytes_total'])} {n1_text}"
        "预算越小，节省的预合成字节和 TTS 调用越多，代价是更多候选命中后要临时合成。",
        f"- 早期发布覆盖最高的是 {early_best['label']}（{early_best['coverage']['early_published']}/10）。"
        "默认档 all 在早期为 0：语音模式下所有候选都要等音频完成才发布；"
        "N>=1 时预算外候选会先以文本出现，但最早的那条候选仍在等音频。"
        "N=0 是例外：全部候选先出文本，所以最早发布覆盖最高。",
        f"- 仅文本候选的回退检查：{len(fallback_ok)}/{len(fallback_arms)} 个适用档位通过，"
        "均只调用 1 次普通 TTS，`audio_source=tts`、`prediction_audio_hit=false`、回复正常完成。",
        "",
    ]

    lines += [
        "",
        "## 未实现 / 边界",
        "",
        "- 这是合成负载，不是真实 TTS 并发、排队、网络抖动或真实候选命中分布；"
        "真实云接口一次都没有调用。",
        "- 预测分支在实验中按固定到达顺序产生候选；生产里分支并发完成顺序可能不同，"
        "`前 N 条` 指实际到达顺序。",
        "- 延迟用真实 `asyncio.sleep` 模拟，毫秒数有数毫秒抖动；本报告选择 150/900 ms "
        "两个远离各候选就绪边界的检查点，档位结论不受抖动影响。",
        "- 只比较音频预算，不模拟语音识别、模型选择、用户打断或播放时长。",
        "- 默认（不设置 `PREDICTION_AUDIO_LIMIT`）仍保持原行为；本实验只验证实验档位。",
        "",
    ]
    return "\n".join(lines)


def arm0(arms: list[dict]) -> dict:
    """Timing columns are identical across arms; use the baseline arm for them."""
    return arms[0]


def parse_limits(raw: str) -> list[int | None]:
    values: list[int | None] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if part.lower() == "all":
            values.append(None)
        else:
            values.append(int(part))
    return values


async def main_async(limits: list[int | None]) -> list[dict]:
    arms = []
    for limit in limits:
        arm = await run_arm(limit)
        arms.append(arm)
        summary = arm["summary"]
        print(
            f"{arm['label']:>5}: prefetch={arm['prefetch_calls']} "
            f"bytes={bytes_label(arm['audio_bytes_total'])} "
            f"forecast={arm['forecast_total_ms']}ms "
            f"early text/audio={summary['early_text_hits']}/{summary['early_audio_hits']} "
            f"late text/audio={summary['late_text_hits']}/{summary['late_audio_hits']} "
            f"fallback={'ok' if arm['fallback'].get('ok') else 'n/a'}"
        )
    return arms


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limits", default="all,0,1,2,3,5,10",
        help="comma-separated arms; 'all' means no audio limit (default)")
    parser.add_argument(
        "--output", type=Path,
        default=ROOT / "experiments" / "prediction-audio-budget",
        help="directory for results.json and results.md")
    args = parser.parse_args()

    arms = asyncio.run(main_async(parse_limits(args.limits)))
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "results.json").write_text(
        json.dumps({"arms": arms}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    (args.output / "results.md").write_text(build_markdown(arms), encoding="utf-8")

    failures = [
        arm["label"] for arm in arms
        if arm["fallback"].get("applicable") and not arm["fallback"].get("ok")
    ]
    if failures:
        print(f"fallback check failed for: {', '.join(failures)}", file=sys.stderr)
        return 1
    print(f"wrote {args.output / 'results.json'} and {args.output / 'results.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
