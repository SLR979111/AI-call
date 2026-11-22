"""One response request: reuse a suitable forecast OR speak directly."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import copy

import httpx
from contextlib import aclosing
from dataclasses import replace
from typing import AsyncIterator

from pydantic import ValidationError

from .forecast import (
    OpeningForecastCache, candidate_limit_from_env, has_unasked_question,
    likely_short_answers, merge_candidates, stream_candidates, ticket_quota,
)
from .phone_file import ROUTE_RULES, Route, compile_phone, model_context
from .phone_style import PHONE_STYLE
from .models import PLAYBOOK_GUIDANCE, Candidate, Memory, Say, TaskCard, Update
from .ticket_forecast import enabled as ticket_forecast_enabled
from .ticket_forecast import excluded_intents_from_env
from .ticket_forecast import generate_ticket_candidates
from .voice_preview import first_clause as pending_speech_clause
from .providers import Cloud, ProviderError
from .context_aware_decision import ContextAwareDecisionEngine
from .decision_model import DecisionModel
from .semantic_decision import shared_matcher


BEHAVIOR = """你是电话客服。按 basic_response 的身份和应答方式，只使用 background 中的业务事实。
graph 是完整业务流程，state.flow 给出这轮的小目标、完成依据和已完成节点。先解决对方最新的追问，若有 repair 先处理理解障碍。对方已经明确的现状不再问一次。操作只说资料给出的入口和步骤。按钮位置、页面状态变化等没有给出的细节不猜。
区分已知记录、假设条件和对方自述。没有依据就说明未知，不编造操作、承诺或渠道。不朗读内部指令。已说明且对方听懂的事情无需重述。
dialogue 中部分播放的句子只能按已听到的部分理解。打断后结合当前问题接话，尊重暂停和结束意愿。
""" + PHONE_STYLE

REPLY_POLICY = """一或两条 {"type":"say","text":"要说的话","covers":[],"question":""}，或一条 {"type":"reuse","id":"候选ID"}。
仅在候选完整回答本轮新诉求时复用。live_lead_ 首句可接一条 say 补答，其他 reuse 单独使用。text 只写口语；涵盖全部条件才标记对应 graph 节点的 checkpoint ID。question 只能摘自实际说出口的问题。
最后输出 {"type":"update","facts":[],"questions":[],"confirmation":"","disposition":"active","disposition_quote":""}。
facts 各项为 {"name":"字段","value":"明确自述","quote":"本轮原话"}。仅记录当事人明确说的事实并更新改口；questions 记录尚待答的问题。confirmation 和所有 quote 必须是本轮原话的连续摘录。
按 state.flow.finished 判定所选沟通目标是否完成。disposition 可取 active/complete/paused/declined。暂停或拒绝需有原话依据。本轮尚未播出的说明不算已告知。对方说还有问题但尚未提出时，请其直接讲。
"""
POLICY = BEHAVIOR + "\n只输出 JSONL。先输出" + REPLY_POLICY

VOICE_ROUTE_POLICY = BEHAVIOR + """
只输出 JSONL。首行必须是 {"type":"route","leaf":"叶子id","completed":[],"reopen":[],"need":"本轮诉求","next_node":null}。route 只供系统使用。
""" + ROUTE_RULES + "\n紧接着用本次选定的路径回答，按本轮已经有证据的节点变动判断完成。输出" + REPLY_POLICY

FORECAST = BEHAVIOR + """
根据刚才的实际回复，为下一轮预测两个不同分支。每个分支只预测一步。when 写完整适用条件，examples 给出一两条满足所有条件的原话。reply.text 直接说具体业务内容，最多100字。不要把简短承接当成已办理或已提交。
只输出 {"candidates":[{"id":"c1","when":"条件","examples":["用户原话"],"reply":{"text":"回复","covers":[],"question":""}}]}。
covers 只填本句完整说明的要点 ID。question 只摘录实际问句。"""
FORECAST_PROMPT_KEY = hashlib.sha256(FORECAST.encode("utf-8")).hexdigest()


class DialogueProtocolError(ProviderError):
    """A model record failed JSONL or dialogue ordering checks."""


class VoiceRouteError(DialogueProtocolError):
    """The combined response failed before any validated speech was available."""


async def json_lines(chunks: AsyncIterator[str], on_first_clause=None) -> AsyncIterator[dict]:
    buffer = ""
    async for chunk in chunks:
        buffer += chunk
        if len(buffer) > 10000:
            raise DialogueProtocolError("模型输出过长")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip()
            if line and not line.startswith("```"):
                try:
                    obj = json.loads(line)
                except ValueError as exc:
                    raise DialogueProtocolError("模型输出格式无效") from exc
                if not isinstance(obj, dict):
                    raise DialogueProtocolError("模型输出格式无效")
                if on_first_clause and obj.get("type") != "route":
                    clause = pending_speech_clause(line)
                    if clause:
                        on_first_clause(clause)
                    on_first_clause = None
                yield obj
        if on_first_clause:
            clause = pending_speech_clause(buffer)
            if clause:
                on_first_clause(clause)
                on_first_clause = None
    tail = buffer.strip()
    if tail and not tail.startswith("```"):
        try:
            obj = json.loads(tail)
        except ValueError as exc:
            raise DialogueProtocolError("模型输出不完整") from exc
        if not isinstance(obj, dict):
            raise DialogueProtocolError("模型输出格式无效")
        yield obj


FORECAST_BRANCHES = (
    "优先覆盖纯肯定/承接（是的、好的、明白了、可以）以及否定/没有。"
    "纯承接的回复应推进尚未说明的内容，不把好的当成材料已提交。",
    "优先覆盖当前话题的具体追问和选择（第一项、怎么补、哪份）。",
    "优先覆盖用户报告的新情况（已准备、已改好、直接授权、已提交）。",
    "优先覆盖用户的纠正、否认和补充条件（不是、我说的是、还没、但是）。",
    "优先覆盖用户忙、稍后再说和结束通话的意愿，同时保留一句可继续的话题。",
)


class Brain:
    def __init__(self, cloud: Cloud, opening_forecasts: OpeningForecastCache | None = None,
                 *, forecast_branches: int = 3, ticket_forecasts: bool | None = None,
                 candidate_limit: int | None = None, decision_model: DecisionModel | None = None,
                 combine_voice_route: bool = False, fresh_forecasts: bool = False):
        self.cloud = cloud
        self.fresh_forecasts = fresh_forecasts
        self.opening_forecasts = None if fresh_forecasts else opening_forecasts
        self.forecast_branches = max(1, min(int(forecast_branches), len(FORECAST_BRANCHES)))
        self.decision_engine = ContextAwareDecisionEngine()
        self.decision_model = decision_model or DecisionModel.from_env(cloud)
        self.ticket_forecasts = False if fresh_forecasts else ticket_forecasts
        self.candidate_limit = candidate_limit
        self.combine_voice_route = combine_voice_route
        # Optional second opinion for the cases the lexical rules reject. It is
        # silent (None) when the embedding model is missing, so the default path
        # never depends on it.
        self.semantic_engine = shared_matcher()
        # Set before each emitted reply so Session can count the decision path
        # (rule vs model vs model text) without parsing model output again.
        self.last_decision: dict | None = None
        self.candidates_considered = False

    def ticket_forecasts_enabled(self) -> bool:
        return (self.ticket_forecasts if self.ticket_forecasts is not None
                else ticket_forecast_enabled())

    def ticket_candidates(self, snapshot: dict) -> list[Candidate]:
        """Deterministic ticket candidates are never stored on disk."""
        if snapshot.get("state", {}).get("flow", {}).get("playbook") or not self.ticket_forecasts_enabled():
            return []
        try:
            card = TaskCard.model_validate(snapshot.get("task"))
        except (ValidationError, TypeError):
            return []
        return generate_ticket_candidates(card)

    @staticmethod
    def snapshot(memory: Memory):
        file = memory.flow.file if memory.flow else compile_phone(memory.card)
        return {"task": memory.card.model_dump(), "briefing": file.briefing(), "state": memory.view(),
                "dialogue": memory.history[-24:]}

    @staticmethod
    def _last_completed_assistant(memory: Memory) -> str:
        """Only an utterance heard in full can anchor a short answer."""
        for entry in reversed(memory.history[:-1]):
            if entry.get("role") == "assistant":
                return entry.get("text", "") if entry.get("delivery") == "complete" else ""
        return ""

    def match_reuse(self, memory: Memory, candidates: list[Candidate]) -> Candidate | None:
        """Return the forecast the deterministic content rules confirm, if any.

        This is the same gate ``respond`` uses for its fast path. Interrupted
        turns may offer staged forecasts to the next user turn, but a model
        decision must never resurrect them.
        """
        if not candidates or not memory.history:
            return None
        if has_unasked_question(memory.history[-1].get("text", "")):
            return None
        if memory.interrupted_position():
            return None
        if memory.history[-1].get("role") != "user":
            return None
        candidates = memory.fresh_candidates(candidates)
        if memory.flow:
            candidates = memory.flow.candidates(candidates)
        decision, selected = self.decision_engine.should_reuse(
            memory.history[-1].get("text", ""),
            [c for c in candidates if not c.id.startswith(("live_lead_", "topic_"))],
            self._last_completed_assistant(memory), memory.card)
        return selected if decision == "reuse" else None

    async def respond(self, memory: Memory, candidates: list[Candidate], *, on_first_clause=None):
        self.last_decision = None
        self.candidates_considered = False
        replay = memory.requested_replay()
        if replay:
            for line in replay:
                yield "say", line
            return
        if memory.history and has_unasked_question(memory.history[-1]["text"]):
            candidates = []
        position = memory.interrupted_position()
        if position:
            recap, followup = position
            # The session appends emitted speech to the live history. Use a
            # separate prompt for the remaining question without changing it.
            previous = memory.history[:-1]
            # A recap is deterministic continuation, not a forecast decision;
            # it must not inflate regeneration/miss counters.
            self.last_decision = {"kind": "say", "path": "recap",
                                  "candidate_id": None}
            yield "say", Say(text=recap)
            if not followup:
                return
            memory = replace(memory, history=[*previous, {"role": "user", "text": followup}])
            candidates = []
        if (self.combine_voice_route and memory.flow and not position
                and not self.semantic_engine and not self.decision_model
                and not self.match_reuse(memory, candidates)):
            available = memory.fresh_candidates(candidates) if not memory.flow.event else []
            try:
                async with aclosing(self._stream_response(memory, available, on_first_clause, route_voice=True)) as events:
                    async for event in events:
                        yield event
                return
            except VoiceRouteError:
                logging.getLogger(__name__).info("Combined voice response rejected; retrying with separate routing")
        if memory.flow and hasattr(self.cloud, "route_phone"):
            try:
                async with asyncio.timeout(12 if getattr(self.cloud, "is_local", False) else 2.5):
                    route = await self.cloud.route_phone(memory.flow, memory.history)
            except (ValueError, RuntimeError, TimeoutError, OSError, httpx.HTTPError) as exc:
                logging.getLogger(__name__).info("Keep current phone goal: %s", type(exc).__name__)
            else:
                if route.leaf in memory.flow.file.graph.leaves():
                    # A speculative voice response works on a copy. Only Session
                    # commits its event after the final transcript is confirmed.
                    memory = replace(memory, flow=copy.deepcopy(memory.flow))
                    memory.flow.apply(route, memory.history[-1]["text"], earlier_users=memory.user_evidence())
                    yield "route", route
        candidates = memory.fresh_candidates(candidates)
        # A forecast only counts as a prediction opportunity when it survived the
        # unasked-question/interruption guards and reached the decision point.
        if memory.flow:
            candidates = memory.flow.candidates(candidates)
        self.candidates_considered = bool(candidates)
        if getattr(self.cloud, "is_local", False):
            kwargs = {"semantic_engine": self.semantic_engine} if self.semantic_engine else {}
            async for kind, value in self.cloud.respond_local(self.snapshot(memory), candidates, on_first_clause, **kwargs):
                if kind == "semantic_reuse":
                    self.last_decision = {"kind": "reuse", "path": "semantic", "candidate_id": value.id}
                    yield "reuse", value.reply
                else:
                    yield kind, value
            return
        # Fast path: Context-aware decision engine (considers assistant's last question type)
        if candidates and memory.history and memory.history[-1].get("role") == "user":
            last_user_input = memory.history[-1].get("text", "")
            assistant_last_text = self._last_completed_assistant(memory)

            eligible = [c for c in candidates if not c.id.startswith(("live_lead_", "topic_"))]
            decision, selected_candidate = self.decision_engine.should_reuse(
                last_user_input, eligible, assistant_last_text, memory.card)
            path = "rule"
            if selected_candidate is None and self.semantic_engine and eligible:
                selected_candidate = await self.semantic_engine.confirm(last_user_input, eligible, assistant_last_text)
                path = "semantic"
            if selected_candidate is None and decision == "llm" and self.decision_model and eligible:
                selected_candidate = await self.decision_model.decide(last_user_input, assistant_last_text, eligible)
                path = "decision_model"

            if selected_candidate is not None:
                # The cached audio is already prepared. Return it before asking
                # for the state update so playback and the next forecast can
                # start while the update request is still in flight.
                self.last_decision = {"kind": "reuse", "path": path,
                                      "candidate_id": selected_candidate.id}
                yield "reuse", selected_candidate.reply
                yield "update_async", selected_candidate
                return
            elif decision == "say":
                candidates = []

        async with aclosing(self._stream_response(memory, candidates, on_first_clause)) as events:
            async for event in events:
                yield event

    async def _stream_response(self, memory, candidates, on_first_clause, *, route_voice=False):
        snapshot = self.snapshot(memory)
        snapshot["candidates"] = [{"id": item.id, "when": item.when, "reply": {"text": item.reply.text}}
                                  for item in candidates]
        if route_voice:
            leaves = memory.flow.file.graph.leaves()
            snapshot.update(leaves=leaves, paths={leaf: memory.flow.path(leaf) for leaf in leaves})
        instruction = VOICE_ROUTE_POLICY if route_voice else POLICY
        if memory.flow and memory.flow.playbook:
            instruction += "\n" + PLAYBOOK_GUIDANCE
        messages = [{"role": "system", "content": instruction},
                    {"role": "user", "content": json.dumps(model_context(snapshot), ensure_ascii=False)}]
        said = 0
        asked = reused = False
        pending_update = None
        pending_route = None
        chat_options = {"max_tokens": 900} if route_voice else {}
        try:
            async with aclosing(self.cloud.chat(messages, **chat_options)) as chunks, aclosing(json_lines(chunks, on_first_clause)) as events:
                async for event in events:
                    kind = event.get("type")
                    if route_voice and not said and pending_route is None:
                        if kind != "route":
                            raise DialogueProtocolError("模型未先返回工单路由")
                        pending_route = Route.model_validate(event)
                        if pending_route.leaf not in leaves:
                            raise DialogueProtocolError("模型选择了无效的工单终点")
                        flow = copy.deepcopy(memory.flow)
                        flow.apply(pending_route, memory.history[-1]["text"], earlier_users=memory.user_evidence())
                        candidates = flow.candidates(candidates)
                        self.candidates_considered = bool(candidates)
                        eligible = [c for c in candidates if not c.id.startswith(("live_lead_", "topic_"))]
                        if eligible:
                            decision, _ = self.decision_engine.should_reuse(
                                memory.history[-1]["text"], eligible,
                                self._last_completed_assistant(memory), memory.card)
                            if decision == "say":
                                candidates = []
                        continue
                    if pending_update is not None:
                        raise DialogueProtocolError("模型在状态更新后继续输出")
                    if kind == "say" and not reused and said < 2 and not asked:
                        say = Say.model_validate(event)
                        if say.question not in say.text:
                            say.question = ""
                        say.covers = [p for p in say.covers if p in {x.id for x in memory.card.checkpoints}]
                        selected = next((c for c in candidates if c.reply.text == say.text), None) if said == 0 else None
                        prepared = selected is not None
                        self.last_decision = {
                            "kind": "reuse" if prepared else "say",
                            "path": ("model_text" if prepared
                                     else ("regenerated" if self.candidates_considered
                                           else "direct")),
                            "candidate_id": selected.id if selected is not None else None,
                        }
                        said += 1
                        asked = bool(say.question)
                        if pending_route is not None:
                            yield "route", pending_route
                            pending_route = None
                        yield "reuse" if prepared else "say", say
                    elif kind == "reuse" and said == 0:
                        match = next((c for c in candidates if c.id == event.get("id")), None)
                        if match is None:
                            raise DialogueProtocolError("模型选择了已失效的预测")
                        self.last_decision = {"kind": "reuse", "path": "model",
                                              "candidate_id": match.id}
                        said = 1
                        reused = not match.id.startswith("live_lead_")
                        asked = bool(match.reply.question)
                        if pending_route is not None:
                            yield "route", pending_route
                            pending_route = None
                        yield "reuse", match.reply
                    elif kind == "update" and said:
                        pending_update = Update.model_validate(event)
                    else:
                        event_type = kind if kind in ("say", "reuse", "update") else "unknown"
                        raise DialogueProtocolError(f"unexpected {event_type} (said={said}, reused={reused}, asked={asked})")
            if not said or pending_update is None:
                raise DialogueProtocolError("模型回复缺少内容或状态更新")
        except (DialogueProtocolError, ValidationError) as exc:
            reason = str(exc) if isinstance(exc, DialogueProtocolError) else "invalid event fields"
            logging.getLogger(__name__).warning("Rejected dialogue record (accepted_speech=%d): %s", said, reason)
            if not said:
                if route_voice:
                    raise VoiceRouteError(reason) from exc
                raise ProviderError("这次回复没能生成，请再说一次。") from exc
            # The validated speech is already queued. Its playback receipt owns
            # delivery credit, and a rejected tail has no authority to change state.
            return
        yield "update", pending_update

    async def update_for_reuse(self, memory: Memory, selected: Candidate) -> Update:
        """Resolve only the state update for an already-played cached reply."""
        snapshot = self.snapshot(memory)
        snapshot["selected_reply"] = selected.reply.model_dump()
        update_messages = [
            {"role": "system", "content": POLICY + "\n本轮已选择 selected_reply 作为回复，请据此只输出一行 update，不再输出 say 或 reuse。是否播完仍以 dialogue 的 delivery 和 state.delivered 为准。"},
            {"role": "user", "content": json.dumps(model_context(snapshot), ensure_ascii=False)},
        ]
        async for event in json_lines(self.cloud.chat(update_messages)):
            if event.get("type") == "update":
                return Update.model_validate(event)
        raise ProviderError("模型回复缺少状态更新")

    async def predict(self, snapshot: dict) -> list[Candidate]:
        return [item async for batch in self.predict_batches(snapshot) for item in batch]

    async def predict_batches(self, snapshot: dict) -> AsyncIterator[list[Candidate]]:
        limit = (self.candidate_limit if self.candidate_limit is not None
                 else candidate_limit_from_env())
        ticket_mode = self.ticket_forecasts_enabled()
        merged: list[Candidate] = []

        def take(items, cap: int | None = None) -> list[Candidate]:
            """Append the fresh part of ``items`` to the shared merged pool."""
            before = len(merged)
            target = limit if cap is None else min(limit, cap)
            merged[:] = merge_candidates(merged, items, target)
            return merged[before:]

        ticket = self.ticket_candidates(snapshot)
        key = None
        cached: list[Candidate] = []
        complete = False
        if self.opening_forecasts is not None:
            # Reuse only under the same model and forecast wording rules.
            # Voice does not change text; audio is always prepared per session.
            model = getattr(getattr(self.cloud, "settings", None), "model", "")
            flow = snapshot.get("state", {}).get("flow", {})
            key = json.dumps({
                "task": snapshot["task"], "model": model,
                "forecast_prompt": FORECAST_PROMPT_KEY,
                "phone_file": flow.get("file"),
                **({"playbook": flow["playbook"]} if flow.get("playbook") else {}),
                # Switching ticket templates, exclusions or the cap changes
                # which model candidates were cached, so A/B arms must not
                # share records.
                "ticket_forecast": ticket_mode,
                "ticket_exclude": sorted(excluded_intents_from_env(TaskCard.model_validate(snapshot.get("task")))),
                "candidate_limit": limit,
            }, sort_keys=True, ensure_ascii=False)
            cached, complete = self.opening_forecasts.get(key)
            if not snapshot.get("opening"):
                # Only task-specific FAQ answers can carry into later turns.
                cached = ([item for item in cached if item.id.startswith("topic_")]
                          if not getattr(self.cloud, "is_local", False) else [])
                key, complete = None, False
        # Deterministic ticket candidates take the first slots, then the cached
        # model forecasts. They may not consume the whole pool: at least
        # ``model_reserve(limit)`` slots stay available for cached/model output.
        # Unused ticket quota automatically flows to the model below.
        quota = ticket_quota(limit)
        ticket_batch = take(ticket, cap=quota) if quota else []
        cached_batch = take(cached)
        generated = list(cached_batch)
        # Separate requests keep common acknowledgements from competing with
        # specific questions for the same two forecast slots. More branches are
        # launched together in production to widen cache coverage.
        branches = FORECAST_BRANCHES[:self.forecast_branches]

        general_branches = len(branches)
        local = getattr(self.cloud, "is_local", False)
        if local:
            branches = branches[:1]
        elif not self.fresh_forecasts and snapshot.get("opening"):
            # Prime two independent FAQ answers per request on the first call.
            # OpeningForecastCache keys every fact and checkpoint in the task.
            topics = [point["description"] for point in snapshot["task"]["checkpoints"]]
            for offset in range(0, len(topics), 2):
                selected = "、".join(topics[offset:offset + 2])
                branches += (f"每个指定事项各预测一个来电者会直接问的具体问题：{selected}。"
                             "每条只用一句具体事实或操作直接答全那个问题，尽量25字以内；不加反问。",)

        queue = asyncio.Queue()
        valid_points = {p["id"] for p in snapshot["task"]["checkpoints"]}
        chat_options = {"use_cache": False} if self.fresh_forecasts and not local else {}

        async def forecast_branch(index: int, instruction: str):
            branch_snapshot = dict(snapshot)
            if index == 0:
                # Anchor the generic branch to actual short utterances instead
                # of letting all requests invent the same specific new facts.
                branch_snapshot["forecast_user_inputs"] = likely_short_answers(snapshot)
                instruction += (
                    "本分支只为 forecast_user_inputs 各生成一条回复。"
                    "when 必须明确包含对应的用户原话；不要附加用户没有说出的事实。"
                    "好的只表示承接，不能推断填错字、更名、已补好或已提交。"
                    "是的只确认上一句明确的是非问句；没有是非问句时按承接处理。"
                )
            if snapshot.get("state", {}).get("flow", {}).get("playbook"):
                instruction = PLAYBOOK_GUIDANCE + "\n" + instruction
            messages = [{"role": "system", "content": FORECAST + "\n" + instruction},
                        {"role": "user", "content": json.dumps(model_context(branch_snapshot), ensure_ascii=False)}]
            try:
                number = 0
                async for item in stream_candidates(self.cloud.chat(messages, max_tokens=600, **chat_options), 2):
                    item.id = f"topic_{index}_{number}" if index >= general_branches else f"p{index}_{number}"
                    number += 1
                    item.reply.covers = [p for p in item.reply.covers if p in valid_points]
                    if item.reply.question not in item.reply.text:
                        item.reply.question = ""
                    queue.put_nowait(item)
            except Exception as exc:
                queue.put_nowait(exc)
            finally:
                queue.put_nowait(None)

        tasks = [] if complete else [
            asyncio.create_task(forecast_branch(i, instruction))
            for i, instruction in enumerate(branches)
        ]
        remaining = len(tasks)
        emitted = False
        error = None
        try:
            if ticket_batch:
                yield ticket_batch
            if cached_batch:
                yield cached_batch
            if complete:
                return
            while remaining:
                item = await queue.get()
                if item is None:
                    remaining -= 1
                elif isinstance(item, Exception):
                    error = item
                else:
                    fresh = take([item])
                    if not fresh:
                        continue
                    emitted = True
                    generated.append(item)
                    if key:
                        self.opening_forecasts.store(key, generated, complete=False)
                    yield fresh
            if not emitted:
                # Preserve the old failure contract for model output: a branch
                # exception is a real forecast failure even though deterministic
                # ticket text may already have been published to the caller.
                if error:
                    raise error
                if not merged:
                    raise ProviderError("所有预测分支均未产生候选")
            if key and not error and emitted:
                self.opening_forecasts.store(key, generated, complete=True)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
