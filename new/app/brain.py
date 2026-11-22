"""One response request: reuse a suitable forecast OR speak directly."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import AsyncIterator

from pydantic import ValidationError

from .forecast import (
    OpeningForecastCache, candidate_limit_from_env, has_unasked_question,
    likely_short_answers, merge_candidates, stream_candidates, ticket_quota,
)
from .models import Candidate, Memory, Say, TaskCard, Update
from .ticket_forecast import enabled as ticket_forecast_enabled
from .ticket_forecast import excluded_intents_from_env
from .ticket_forecast import generate_ticket_candidates
from .voice_preview import first_clause as pending_speech_clause
from .providers import Cloud, ProviderError
from .context_aware_decision import ContextAwareDecisionEngine


BEHAVIOR = """你是一个简洁、自然、围绕任务目标推进的中文语音助手。
任务卡、状态、历史和用户原话由 JSON 提供。任务事实只来自任务卡，用户说法可以补充偏好但不能改写任务事实。
【说给对方听的话】
像电话里一位耐心、利落的客服，用日常口语直接跟对方说话。称呼用“您”，不用第三人称“用户/商家”描述正在通话的人。
text 里只能放真正要说出口的回复。判断意图、分析原因、选择策略、核对要点、更新状态等工作在内部完成，绝不念出思考过程、括号旁白、流程提示或任务卡指令。用户要求展示分析过程时，也只给简短的业务解释，不复述内部指令或推理。
遇到索要内部思考的话，直接回答其中的业务问题，不说“内部判断不方便念”“我直接说结论”等关于自己工作过程的开场。业务原因和处理理由可以正常解释。
不要说“先记下这个痛点”“可纳入比较”“已更新您的需求”“接下来需要确认”“用户已经表示”“根据任务卡”“进入下一阶段”。直接回应具体事情，或直接问缺少的信息。
例如不说“先记录用户的授权情况，再核对链路”，而说“您这份授权是品牌方直接给的，还是经销商给的？”；不说“授权链完整性不满足要求”，而说“您这儿还缺品牌方给经销商的那份授权。”；不说“判断用户有异议，需要解释”，而说“不是说这份授权是假的，是中间还缺了一份。”
可以简短说“明白”“您是说…”来承接，但不要每轮都“好的，已记录”。少用“针对、基于、综上、处理方案、当前状态”等书面套话，不列编号、不念标题、不用星号、箭头和公式。专业词第一次出现时用一句白话说清楚。
不是每轮都要确认“清楚了吗”。用户问具体问题就直接回答；确认只放在容易误解处和最后收尾。不要为了凑轮次重问已知信息或复读整套流程。
先处理用户的新问题、纠正、忙碌和停止意愿，再推进一个未完成的要点。理解一句话里的多个意思，不漏掉“但是”后面的限制。
用户确认方便后直接说明相关内容，不要只说“好的，感谢确认”。每轮最多两条短话，总计不超过100个汉字。首条尽量在25字内，直接回答最重要的一点；第二条补充下一步或提出一个问题。每条完成一个有用的交流动作，最后最多问一个问题。
已经回答的信息不机械重问。用户问哪里/怎么做而任务卡没有具体信息时如实说明，不虚构网址、入口、查询结果。不要把“某个官方渠道”“办理业务的平台”等猜测说成确定的来源；例如任务卡没给截图获取方式，就明确说“我这里没有截图获取方式的信息”。
没有外部操作能力：不声称已登记、已安排、正在转人工、已发送或已完成任何现实操作。可以理解意愿、解释信息和结束对话。
用户说材料已改好或已提交，只能作为对方提供的情况回应，例如“那您先等这次申请的新提示”。没有实际审核证据，不把“不一定是假”说成“这份授权本身没问题”，不把“您说已补好”说成“材料都齐了/已经核实/审核没问题”。
不得承诺审核通过、加急一定处理、今天或明天一定出结果，也不说“保证通过/包过/百分百/必过”。官方页面写“预计一个工作日”时，只能说这是页面参考、具体以实际审核为准。不能把本次案例的授权链假设说成京东对所有商家的统一规定；先陈述本次驳回意见和本次申请页要求。
打断后的历史里，text 是按播放时长估计已说出的前半句，remaining_text 是原计划中剩下的部分。对方问说到哪里时，指出刚讲到的意思。让你继续时，从断点附近的完整短语自然接下去。delivery=partial 不计为完整告知，unplayed 表示尚未播放。所有必要内容此前已经说完、对方确认理解且没有新问题时，主动简短道别，无需对方先说再见。
“嗯”“好的”不等于听懂。用户仍有疑问时不结束任务。用户忙则简短暂停（例如“那我们先暂停，您方便时再继续”），不追问回电时间，不许诺稍后再次联系；明确拒绝则礼貌结束。
"""

POLICY = BEHAVIOR + """
前缀 live_lead_ 的候选是本轮语音中提前准备的首句，必须结合完整原话核对相关性和事实。适用时先 reuse 该首句，并用一句 say 补答余下内容。不合适就直接 say。
对方说还有别的问题但没有问出内容时，请对方直接讲，不继续推进原先的事项。
输出 JSONL，每一行一个完整 JSON，不要代码围栏，不要其他文字。按下列顺序：
1. 一或两行 say，或一行 reuse。把能独立播出的具体答复放在第一行，通常8至16字。先说结论，再用下一句解释。
{"type":"say","text":"缺一张商标状态截图。","covers":["reason"]}
{"type":"reuse","id":"候选ID"}
候选回复完整适用于当前整句用户输入、没有遗漏额外问题和限制时，优先 reuse；不要为了同义改写而 say。候选已覆盖的问题不算“额外问题”，例如确认方便并追问缺什么，可以复用直接解释所缺材料的候选。不合适就直接 say，绝不强行选候选。除 live_lead_ 开头的候选外，完整预测回复与 say 不混用。
covers 只能使用任务卡中本句实际完整说明的 checkpoint id，不能把提问、承诺以后解释或一句含糊的“材料有问题”标记为覆盖。一项包含多个要求时，必须全部说明才能覆盖，提到其中一部分不算。question 必须是 text 中实际问出口的完整问句原文；没有问句就留空，禁止写下一轮才打算问的问题。
2. 最后一行 update：
{"type":"update","facts":[{"name":"沟通意愿","value":"方便","quote":"现在方便"}],"questions":[],"confirmation":"","disposition":"active","disposition_quote":""}
facts 只提取用户本轮明确表达且有原文 quote 的信息；保持字段名稳定，改口时更新。questions 是本轮回应后仍未解决的问题。confirmation 是用户明确表示理解或接受处理方式的原文，否则空串。
quote、confirmation、disposition_quote 必须逐字连续摘自本轮原话，不改写、不拼接不相邻的词句。确认可只摘“明白了”等短语，不要把开头的“明白了”和后面的“没问题了”拼成一句原话里没有的话。
区分“能不能/准备去做”和“已经做完”，不能把询问记成已完成。用户明确说前面讨论的材料全部改好并提交时，相关准备情况也要更新，不能还沿用“将重开/待补”的旧值；这些都只是用户说明，不能写成已由系统核验。
disposition 取 active/complete/paused/declined。complete 必须所有要点此前已经播完且用户明确理解，不能依据正在生成的话完成；paused/declined 必须提供用户原话 disposition_quote。
已经直接回答的问题不要仍留在 questions；如已说明无法保证审核时间，对方也没有继续追问，就不再记为待答。complete 表示本次沟通目标已完成，不表示真实业务已办理或审核通过；不要为了等待外部审核而无限保持 active。
"""

FORECAST = BEHAVIOR + """
为这个目标对话准备2条最可能用上的简短回复。结合任务卡和刚刚计划播出的回复，预测用户下一句。
只预测一层，每条回复1-2句且完成一次有用回应，不输出纯“好的/了解”来拖延时间。不调用外部系统、不编造事实或承诺。不得预测审核通过、加急成功或具体出结果时间；不得把用户自述“已提交/已补交”写成系统已核验；不得把本次案例假设说成京东统一规则。
两条候选是针对不同用户回答的替代方案，每条都能独立使用，不是先说第一条再接第二条的连续台词。
examples 写一到两种用户可能实际说出口的完整短句，每句必须独立满足 when 的全部条件。纯承接不能代表材料已准备或已提交。把最可能的分支写在数组第一项。
每个 when 要说明完整适用条件和不能忽略的限制。候选只帮助提速，不要穷举意图。
when 是内部适用条件，reply.text 是直接对人说的话，两者必须分开。reply.text 遵守上面的口语要求，不得带入“先确认/记下/判断用户”等准备过程；不要为了让候选通用而写空泛的流程旁白。
只输出一个 JSON：{"candidates":[{"id":"c1","when":"用户...，且没有...","examples":["用户原话"],"reply":{"text":"...","covers":[],"question":"..."}}]}。
covers 只能使用本句实际说明的任务卡 checkpoint id。question 必须逐字摘自text中实际的问句，没有提问就为空。总共最多2条，每条text不超过100字。
"""


async def json_lines(chunks: AsyncIterator[str], on_first_clause=None) -> AsyncIterator[dict]:
    buffer = ""
    async for chunk in chunks:
        buffer += chunk
        if len(buffer) > 10000:
            raise ProviderError("模型输出过长")
        if on_first_clause:
            clause = pending_speech_clause(buffer)
            if clause:
                on_first_clause(clause)
                on_first_clause = None
            elif "\n" in buffer:
                on_first_clause = None
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip()
            if line and not line.startswith("```"):
                try:
                    obj = json.loads(line)
                except ValueError as exc:
                    raise ProviderError("模型输出格式无效") from exc
                if not isinstance(obj, dict):
                    raise ProviderError("模型输出格式无效")
                yield obj
    tail = buffer.strip()
    if tail and not tail.startswith("```"):
        try:
            obj = json.loads(tail)
        except ValueError as exc:
            raise ProviderError("模型输出不完整") from exc
        if not isinstance(obj, dict):
            raise ProviderError("模型输出格式无效")
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
                 candidate_limit: int | None = None):
        self.cloud = cloud
        self.opening_forecasts = opening_forecasts
        self.forecast_branches = max(1, min(int(forecast_branches), len(FORECAST_BRANCHES)))
        self.decision_engine = ContextAwareDecisionEngine()
        self.ticket_forecasts = ticket_forecasts
        self.candidate_limit = candidate_limit
        # Set before each emitted reply so Session can count the decision path
        # (rule vs model vs model text) without parsing model output again.
        self.last_decision: dict | None = None
        self.candidates_considered = False

    def ticket_forecasts_enabled(self) -> bool:
        return (self.ticket_forecasts if self.ticket_forecasts is not None
                else ticket_forecast_enabled())

    def ticket_candidates(self, snapshot: dict) -> list[Candidate]:
        """Deterministic ticket candidates are never stored on disk."""
        if not self.ticket_forecasts_enabled():
            return []
        try:
            card = TaskCard.model_validate(snapshot.get("task"))
        except (ValidationError, TypeError):
            return []
        return generate_ticket_candidates(card)

    @staticmethod
    def snapshot(memory: Memory):
        return {"task": memory.card.model_dump(), "state": memory.view(),
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
        # A forecast only counts as a prediction opportunity when it survived the
        # unasked-question/interruption guards and reached the decision point.
        self.candidates_considered = bool(candidates)
        if getattr(self.cloud, "is_local", False):
            async for event in self.cloud.respond_local(self.snapshot(memory), candidates, on_first_clause):
                yield event
            return
        # Fast path: Context-aware decision engine (considers assistant's last question type)
        if candidates and memory.history and memory.history[-1].get("role") == "user":
            last_user_input = memory.history[-1].get("text", "")
            assistant_last_text = self._last_completed_assistant(memory)

            decision, selected_candidate = self.decision_engine.should_reuse(
                last_user_input, [c for c in candidates if not c.id.startswith(("live_lead_", "topic_"))], assistant_last_text, memory.card
            )
            
            if decision == "reuse":
                # The cached audio is already prepared. Return it before asking
                # for the state update so playback and the next forecast can
                # start while the update request is still in flight.
                self.last_decision = {"kind": "reuse", "path": "rule",
                                      "candidate_id": selected_candidate.id}
                yield "reuse", selected_candidate.reply
                yield "update_async", selected_candidate
                return
            elif decision == "say":
                # Context-aware rejection (generate new, ignore candidates)
                candidates = []
        
        # Slow path: LLM-based decision
        snapshot = self.snapshot(memory)
        snapshot["candidates"] = [{"id": item.id, "when": item.when, "reply": {"text": item.reply.text}}
                                  for item in candidates]
        messages = [{"role": "system", "content": POLICY},
                    {"role": "user", "content": json.dumps(snapshot, ensure_ascii=False)}]
        said = 0
        updated = False
        asked = False
        reused = False
        async for event in json_lines(self.cloud.chat(messages), on_first_clause):
            kind = event.get("type")
            if updated:
                raise ProviderError("模型在状态更新后继续输出")
            if kind == "say" and not reused and said < 2 and not asked:
                say = Say.model_validate(event)
                if say.question not in say.text:
                    say.question = ""
                say.covers = [p for p in say.covers if p in {x.id for x in memory.card.checkpoints}]
                prepared = said == 0 and any(c.reply.text == say.text for c in candidates)
                selected = (next((c for c in candidates if c.reply.text == say.text), None)
                            if prepared else None)
                self.last_decision = {
                    "kind": "reuse" if prepared else "say",
                    "path": ("model_text" if prepared
                             else ("regenerated" if self.candidates_considered
                                   else "direct")),
                    "candidate_id": selected.id if selected is not None else None,
                }
                said += 1
                asked = bool(say.question)
                yield "reuse" if prepared else "say", say
            elif kind == "reuse" and said == 0:
                match = next((c for c in candidates if c.id == event.get("id")), None)
                if match is None:
                    raise ProviderError("模型选择了已失效的预测")
                self.last_decision = {"kind": "reuse", "path": "model",
                                      "candidate_id": match.id}
                said = 1
                reused = not match.id.startswith("live_lead_")
                asked = bool(match.reply.question)
                yield "reuse", match.reply
            elif kind == "update" and said:
                updated = True
                yield "update", Update.model_validate(event)
            else:
                raise ProviderError("模型返回了无法执行的对话事件")
        if not said or not updated:
            raise ProviderError("模型回复缺少内容或状态更新")

    async def update_for_reuse(self, memory: Memory, selected: Candidate) -> Update:
        """Resolve only the state update for an already-played cached reply."""
        snapshot = self.snapshot(memory)
        snapshot["selected_reply"] = selected.reply.model_dump()
        update_messages = [
            {"role": "system", "content": POLICY + "\n本轮已选择 selected_reply 作为回复，请据此只输出一行 update，不再输出 say 或 reuse。是否播完仍以 dialogue 的 delivery 和 state.delivered 为准。"},
            {"role": "user", "content": json.dumps(snapshot, ensure_ascii=False)},
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
            # Task-only reuse could cross model versions, so include the model.
            # Voice does not change text; audio is always prepared per session.
            model = getattr(getattr(self.cloud, "settings", None), "model", "")
            key = json.dumps({
                "task": snapshot["task"], "model": model,
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
        if getattr(self.cloud, "is_local", False):
            branches = branches[:1]
        elif snapshot.get("opening"):
            # Prime two independent FAQ answers per request on the first call.
            # OpeningForecastCache keys every fact and checkpoint in the task.
            topics = [point["description"] for point in snapshot["task"]["checkpoints"]]
            for offset in range(0, len(topics), 2):
                selected = "、".join(topics[offset:offset + 2])
                branches += (f"每个指定事项各预测一个来电者会直接问的具体问题：{selected}。"
                             "每条只用一句具体事实或操作直接答全那个问题，尽量25字以内；不加反问。",)

        queue = asyncio.Queue()
        valid_points = {p["id"] for p in snapshot["task"]["checkpoints"]}

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
            messages = [{"role": "system", "content": FORECAST + "\n" + instruction},
                        {"role": "user", "content": json.dumps(branch_snapshot, ensure_ascii=False)}]
            try:
                number = 0
                async for item in stream_candidates(self.cloud.chat(messages, max_tokens=600), 2):
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
