"""Opt-in POP phone path with ordered evidence for the competition's six stages."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path
from time import perf_counter

from .pop_contextual import DEFINITION, REVIEW_MEANING, CONFIRM_QUESTION, FollowupContext, reply as contextual_reply
from .pop_grounded_answer import case_evidence
from .pop_guidance import guidance_reply, guidance_topics
from .pop_repair import GUIDED, RepairPolicy, asks_for_explanation, followup_sentences, needs_replay, topics_for
from .pop_resilient import (
    ASK_SHOP, ASSENT, CASE_QUESTION, DECLINE_TRANSFER, FAREWELL, FAREWELL_REPLY,
    GENERAL_REVIEW, REVIEW_EXPLANATION, REVOKED, UNAVAILABLE, masked_shop,
)
from .pop_strategy import (
    PopStrategyAgent, _FORMER_CONTACT, _HUMAN, _OPT_OUT, _WRONG_PERSON,
)

STAGES = ("OPEN", "IDENTITY_CONFIRM", "NEED_CONFIRM", "OFFER", "RESULT_CONFIRM", "WRITEBACK")
SEPARATORS = re.compile(r"[\s,。!、;：:…]+")
FILLER = re.compile(r"^(?:嗯+|呃+|那个|这个|您好|你好)")
UNCERTAIN = re.compile(r"[?吗]|是不是|对不对|不确定|说不准|应该|可能|好像|大概|吧$")
YES = r"(?:(?:是的?|对的?|没错)(?:啊|呀|嗯)?){1,3}"
SPEAK = r"(?:(?:您|你|请)(?:说|讲)(?:吧|啊)?|(?:说|讲)吧)"
SHOP = r"(?:(?:这家|这间|该|本)?店铺?)"
ROLE = r"(?:负责人|申请人|店主|法人|老板)"
IDENTITY = re.compile(
    rf"(?:{YES})?(?:本人(?:在接听)?|是本人|我就是|(?:就)?是我"
    rf"|我(?:就)?是(?:(?:{SHOP}|店里|这边|这里)的?)?{ROLE}"
    rf"|我(?:在)?(?:负责|管理){SHOP}|{SHOP}(?:是)?我(?:在)?(?:负责|管)(?:的)?"
    rf"|{SHOP}(?:归|由)我(?:负责|管))(?:啊|呀)?"
)
IDENTITY_NO = re.compile(r"^(?:不是(?:的|啊|呀)?|不对|非本人)$|不是.{0,6}(?:店主|老板|法人)|代接|替.{1,5}接(?:的)?电话")
AVAILABLE = re.compile(
    rf"(?:我)?(?:现在|这会儿)?(?:方便|有空|不忙)(?:的|啊|呀)?(?:{SPEAK}|可以说|可以讲)?"
    rf"|(?:行|好(?:的)?|可以)(?:啊|呀)?(?:{SPEAK}|说|讲)?|{SPEAK}"
)
UNDERSTOOD = re.compile(
    r"(?:好(?:的)?)?(?:我)?(?:已经)?(?:听明白|明白|听懂|懂|理解|清楚|知道)(?:了|啦)?"
    r"(?:谢谢)?|(?:我)?(?:知道怎么补(?:材料)?了|会按(?:你|您)说的补(?:材料)?)"
)
CONTEXTUAL_ACK = re.compile(r"(?:好(?:的)?|行){0,3}(?:我)?(?:已经)?(?:搞懂|弄明白|听懂|听明白|明白|清楚|懂)(?:了|啦)?")
STOP = re.compile(r"不(?:太)?方便|不想继续|先不说|不要记录|别(?:登记|记录)")
WITHHOLD = re.compile(r"(?:别|不用|先不|不必|不要|暂不)(?:给我|帮我)?(?:记|写回|提交|登记|录音)")
BUSY = re.compile(r"(?<!不)忙|没(?:空|时间)|(?:晚点|稍后|改天|待会儿?)(?:再)?(?:聊|说)")
OPENING = "您好，我是京东商家资质审核AI客服。"
RECORDING = "本次通话会录音。请问您是这家店的负责人吗？"
VERIFY_AGAIN = "请问您是这家店的负责人吗？"
ASK_AVAILABILITY = "请问您现在方便沟通资质审核吗？"
AUDIT_RESULT = "这笔资质申请未通过。"
RECORDED = "沟通结果已记录。感谢接听。"
PRIME_SENTENCES = (OPENING, RECORDING, VERIFY_AGAIN, ASK_AVAILABILITY, AUDIT_RESULT, RECORDED)


def normalize_answer(text: str) -> str:
    compact = SEPARATORS.sub("", unicodedata.normalize("NFKC", text))
    return FILLER.sub("", compact, count=1)


def identity_answer(text: str) -> str:
    """Interpret an answer only in the context of the agent's identity question."""
    text = normalize_answer(text)
    if re.search(r"[?吗]|是不是|对不对", text):
        return "unclear"
    if IDENTITY_NO.search(text) or _WRONG_PERSON.search(text) or _FORMER_CONTACT.search(text):
        return "denied"
    answer = re.sub(rf"{SPEAK}$", "", text)
    # Natural speech often appends "方便/您说/现在" after the role declaration.
    # Match the identity phrase as a prefix instead of requiring the whole turn
    # to be exactly "我是负责人".
    if not UNCERTAIN.search(answer) and (
        re.fullmatch(YES, answer)
        or re.fullmatch(r"(?:可以|行|好(?:的)?)", answer)
        or IDENTITY.search(answer)
    ):
        return "confirmed"
    return "unclear"


def result_confirmed(text: str) -> bool:
    return bool(UNDERSTOOD.fullmatch(normalize_answer(text)))


@dataclass
class Contact:
    phase: str = "open"
    slots: dict | None = None
    source: dict | None = None
    writeback: dict | None = None
    spoken_reason: str = ""
    followup: FollowupContext = field(default_factory=FollowupContext)
    last_public: str = ""
    case_context: dict | None = None


class PopCompetitionAgent(PopStrategyAgent):
    opens_outbound = True

    def __init__(self, knowledge_path: Path, context: dict, store, *, policy: RepairPolicy = GUIDED,
                 semantic_topics=None, grounded_answer=None, case_contexts: dict | None = None,
                 scene_policy=None) -> None:
        super().__init__(knowledge_path, 0, identity_first=True, case_grounding=True,
                         callback_receipt=True, answer_followups=True)
        self.context = context
        self.case_contexts = case_contexts or {}
        self.case_docs = {doc["id"]: doc for doc in self.knowledge.docs}
        self.store = store
        self.policy = policy
        self.semantic_topics = semantic_topics
        self.grounded_answer = grounded_answer
        self.scene_policy = scene_policy
        self.contacts: dict[str, Contact] = {}

    async def start_session(self, session_id: str, version_set: dict[str, str], flow_pack: dict) -> None:
        self.contacts[session_id] = Contact(case_context=self.case_contexts.get(flow_pack.get("case_id")))

    def _case_doc(self, context: dict) -> dict | None:
        if "doc_id" in context:
            return self.case_docs.get(context["doc_id"])
        return self._find_case_doc(context)

    async def _repair_reply(self, text: str, state: Contact):
        guided = self.policy.guided_remediation
        topics = guidance_topics(text) if guided else topics_for(text)
        source = "local"
        timing = {}
        if not topics:
            if needs_replay(text):
                topics = ("reason", "action")
            elif self.semantic_topics:
                started = perf_counter()
                topics, source = await self.semantic_topics(text)
                timing = {"semantic_ms": round((perf_counter() - started) * 1000, 1)}
        if not topics:
            topics = ("reason", "action")
            source = f"{source}_recap"
        render = guidance_reply if guided else followup_sentences
        sentences = render(topics, reason=state.spoken_reason, action=state.slots["remediation"])
        if topics not in {("private",), ("approval",)}:
            sentences.append("这样讲清楚了吗？还有哪一点需要我再说明？")
        return sentences, {"topics": list(topics), "source": source, **timing}

    async def handle(self, req, cancel, tools):
        session_id = req["session_id"]
        state = self.contacts.setdefault(session_id, Contact())
        context = state.case_context or self.context
        reply = req["reply_id"]
        text = str(req["user_text"]).strip()
        repair = None
        next_followup = None
        contextual = self.policy.contextual_followups
        resilient = self.policy.resilient_dialogue
        normalized = normalize_answer(text)
        confirmed = result_confirmed(text) or (contextual and CONTEXTUAL_ACK.fullmatch(normalize_answer(text)))
        needs_identity = state.phase in {"open", "identity"}
        answer = identity_answer(text) if needs_identity else None
        if resilient and FAREWELL.fullmatch(text):
            state.phase = "stopped"
            sentences, outcome, stage = [FAREWELL_REPLY], "safe_exit", None
        elif (state.phase == "stopped" or STOP.search(text) or _OPT_OUT.search(text)
                or (self.policy.repair_followups and WITHHOLD.search(text))
                or answer == "denied" or (resilient and REVOKED.search(normalized))):
            state.phase = "stopped"
            sentences, outcome, stage = ["为保护商家信息，本次通话到这里。谢谢接听。"], "safe_exit", None
        elif _HUMAN.search(text) and not (resilient and DECLINE_TRANSFER.search(text)):
            state.phase = "stopped"
            yield {"kind": "transfer", "reply_id": reply, "reason": "user_request", "queue": "human"}
            return
        elif state.phase == "open":
            # The first user turn may already say "我是负责人，方便". Do not
            # throw that identity away and ask the same question again.
            if text.strip() and identity_answer(text) == "confirmed":
                yield {"kind": "stage", "reply_id": reply, "stage": "IDENTITY_CONFIRM"}
                state.phase = "need"
                sentences, outcome, stage = [ASK_AVAILABILITY], "pending_need", None
            else:
                sentences = [OPENING, RECORDING]
                outcome, stage = "pending_identity", ("OPEN", "identity")
        elif state.phase == "identity":
            if answer == "confirmed":
                yield {"kind": "stage", "reply_id": reply, "stage": "IDENTITY_CONFIRM"}
                state.phase = "need"
                sentences, outcome, stage = [ASK_AVAILABILITY], "pending_need", None
            elif resilient and ASK_SHOP.search(text):
                sentences = [masked_shop(str(context.get("brand", ""))), VERIFY_AGAIN]
                outcome, stage = "pending_identity", None
            else:
                sentences, outcome, stage = [VERIFY_AGAIN, "您可以直接回答是的，或者不是。"], "pending_identity", None
        elif state.phase == "need":
            guidance = guidance_topics(text) if self.policy.guided_remediation else ()
            if guidance in {("private",), ("approval",)}:
                guidance = ()
            allowed = bool(AVAILABLE.fullmatch(normalized) or guidance
                           or (self.policy.answer_implied_need and asks_for_explanation(text)))
            if resilient:
                allowed = bool(AVAILABLE.fullmatch(normalized) or ASSENT.fullmatch(normalized) or CASE_QUESTION.search(text))
            if BUSY.search(normalized) or (contextual and UNAVAILABLE.search(text)):
                state.phase = "stopped"
                sentences, outcome, stage = ["好的，感谢接听。再见。"], "safe_exit", None
            elif resilient and GENERAL_REVIEW.search(text):
                sentences, outcome, stage = [REVIEW_EXPLANATION, ASK_AVAILABILITY], "pending_need", None
            elif contextual and DEFINITION.search(text):
                sentences, outcome, stage = [REVIEW_MEANING, ASK_AVAILABILITY], "pending_need", None
            elif allowed:
                explanation, route_outcome, slots, source = self._explain(text, self._case_doc(context), context)
                if route_outcome != "rejected":
                    sentences, outcome, stage = ["请以京麦的书面审核意见为准。"], "route_unknown", None
                else:
                    yield {"kind": "stage", "reply_id": reply, "stage": "NEED_CONFIRM"}
                    state.slots, state.source = slots, source
                    reason = explanation[0].split("原因是", 1)[-1]
                    state.spoken_reason = reason.rstrip("。")
                    if resilient:
                        next_followup = FollowupContext(focus="reason")
                    sentences = [AUDIT_RESULT, f"审核问题是{reason}"]
                    if guidance and set(guidance) - {"reason", "action"}:
                        topics = tuple(topic for topic in guidance if topic != "reason")
                        sentences.extend(guidance_reply(topics, reason=state.spoken_reason,
                                                       action=slots["remediation"]))
                        if not set(topics) & {"process", "material", "action"}:
                            sentences.append(f"请{slots['remediation'].removeprefix('请')}。")
                    else:
                        sentences.append(f"请{slots['remediation']}。")
                    sentences.append("结果以复审为准。您听明白整改办法了吗？")
                    outcome, stage = "pending_result", ("OFFER", "confirm")
            else:
                sentences, outcome, stage = ["您现在方便听一下资质审核结果吗？"], "pending_need", None
        elif state.phase == "done":
            sentences, outcome, stage = ["本次沟通结果已记录。感谢接听。"], "pop_contact_recorded", None
        elif contextual and confirmed and not state.followup.can_confirm:
            sentences, outcome, stage = [CONFIRM_QUESTION], "pending_result", None
            next_followup = FollowupContext()
        elif confirmed:
            yield {"kind": "stage", "reply_id": reply, "stage": "RESULT_CONFIRM"}
            cancel.check()
            result = await self.store.record(req, request_id=state.slots["request_id"],
                                             doc_id=state.source["citation"]["doc_id"],
                                             route=state.slots["handling_mode"])
            if result:
                state.phase, state.writeback = "done", result
                yield {"kind": "stage", "reply_id": reply, "stage": "WRITEBACK"}
                sentences, outcome, stage = [RECORDED], "pop_contact_recorded", None
            else:
                sentences, outcome, stage = ["这次没能记录沟通结果，请在京麦查看审核意见。"], "writeback_failed", None
        elif contextual:
            sentences, repair, next_followup = await contextual_reply(
                text, state.followup, reason=state.spoken_reason, action=state.slots["remediation"],
                doc_id=state.source["citation"]["doc_id"], semantic_topics=self.semantic_topics,
                fast=self.policy.fast_followups,
                resilient=resilient,
                scene_policy=self.scene_policy if resilient else None,
            )
            generator_routes = {"focused_clarification"} if resilient else {"focused_clarification", "plain_rephrase"}
            if not self.policy.fast_followups:
                generator_routes.add("grounded_followup")
            if self.grounded_answer and repair["source"] in generator_routes:
                generated, details = await self.grounded_answer(
                    text, facts=case_evidence(state.spoken_reason, state.slots["remediation"]), previous=state.last_public,
                )
                repair.update(details)
                if generated:
                    sentences = [generated]
                    repair["source"] = "grounded_generation"
                    next_followup = FollowupContext(step=next_followup.step, can_confirm=False, focus=next_followup.focus)
                elif self.policy.fast_followups and not resilient and repair["source"] == "focused_clarification":
                    sentences = ["这项细节我暂时无法核实，请先看京麦原申请里的书面意见。",
                                 "您主要卡在材料要求，还是提交操作？"]
            outcome, stage = "pending_result", None
        elif self.policy.repair_followups:
            sentences, repair = await self._repair_reply(text, state)
            outcome, stage = "pending_result", None
        else:
            explanation, _, _, _ = self._explain(text, self._case_doc(context), context)
            sentences = self._followup(text, explanation, state.slots)
            sentences.append("请问您确认听明白整改办法了吗？")
            outcome, stage = "pending_result", None

        if resilient and state.phase == "confirm" and next_followup is not None:
            cancel.check()
            # Keep the question's topic after an interruption, but require a complete explanation before confirmation.
            state.followup = replace(state.followup, focus=next_followup.focus, can_confirm=False)
        for index, sentence in enumerate(sentences, start=1):
            cancel.check()
            yield {"kind": "sentence", "reply_id": reply, "text": sentence,
                   "sentence_id": index, "final": index == len(sentences)}
        if stage:
            reached, state.phase = stage
            yield {"kind": "stage", "reply_id": reply, "stage": reached}
        if next_followup is not None:
            cancel.check()
            state.followup = next_followup
        if self.grounded_answer and state.phase == "confirm":
            state.last_public = "".join(sentences)[:140]
        result = dict(state.source or {})
        if repair:
            result["followup"] = repair
        if state.writeback:
            result["writeback"] = state.writeback
        yield {"kind": "end", "reply_id": reply, "outcome": outcome, "slots": state.slots or {}, "result": result}
