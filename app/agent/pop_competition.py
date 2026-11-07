"""Opt-in POP phone path with ordered evidence for the competition's six stages."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .pop_guidance import guidance_reply, guidance_topics
from .pop_repair import GUIDED, RepairPolicy, asks_for_explanation, followup_sentences, needs_replay, topics_for
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
    if not UNCERTAIN.search(answer) and (re.fullmatch(YES, answer) or IDENTITY.fullmatch(answer)):
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


class PopCompetitionAgent(PopStrategyAgent):
    opens_outbound = True

    def __init__(self, knowledge_path: Path, context: dict, store, *, policy: RepairPolicy = GUIDED,
                 semantic_topics=None) -> None:
        super().__init__(knowledge_path, 0, identity_first=True, case_grounding=True,
                         callback_receipt=True, answer_followups=True)
        self.context = context
        self.store = store
        self.policy = policy
        self.semantic_topics = semantic_topics
        self.contacts: dict[str, Contact] = {}

    async def _repair_reply(self, text: str, state: Contact):
        guided = self.policy.guided_remediation
        topics = guidance_topics(text) if guided else topics_for(text)
        source = "local"
        if not topics:
            if needs_replay(text):
                topics = ("reason", "action")
            elif self.semantic_topics:
                topics, source = await self.semantic_topics(text)
        if not topics:
            topics = ("reason", "action")
            source = f"{source}_recap"
        render = guidance_reply if guided else followup_sentences
        sentences = render(topics, reason=state.spoken_reason, action=state.slots["remediation"])
        if topics not in {("private",), ("approval",)}:
            sentences.append("这样讲清楚了吗？还有哪一点需要我再说明？")
        return sentences, {"topics": list(topics), "source": source}

    async def handle(self, req, cancel, tools):
        session_id = req["session_id"]
        state = self.contacts.setdefault(session_id, Contact())
        reply = req["reply_id"]
        text = str(req["user_text"]).strip()
        repair = None
        needs_identity = state.phase in {"open", "identity"}
        answer = identity_answer(text) if needs_identity else None
        if (state.phase == "stopped" or STOP.search(text) or _OPT_OUT.search(text)
                or (self.policy.repair_followups and WITHHOLD.search(text))
                or answer == "denied"):
            state.phase = "stopped"
            sentences, outcome, stage = ["为保护商家信息，本次通话到这里。谢谢接听。"], "safe_exit", None
        elif _HUMAN.search(text):
            state.phase = "stopped"
            yield {"kind": "transfer", "reply_id": reply, "reason": "user_request", "queue": "human"}
            return
        elif state.phase == "open":
            sentences = [OPENING, RECORDING]
            outcome, stage = "pending_identity", ("OPEN", "identity")
        elif state.phase == "identity":
            if answer == "confirmed":
                yield {"kind": "stage", "reply_id": reply, "stage": "IDENTITY_CONFIRM"}
                state.phase = "need"
                sentences, outcome, stage = [ASK_AVAILABILITY], "pending_need", None
            else:
                sentences, outcome, stage = [VERIFY_AGAIN, "您可以直接回答是的，或者不是。"], "pending_identity", None
        elif state.phase == "need":
            guidance = guidance_topics(text) if self.policy.guided_remediation else ()
            if guidance in {("private",), ("approval",)}:
                guidance = ()
            if BUSY.search(normalize_answer(text)):
                state.phase = "stopped"
                sentences, outcome, stage = ["好的，感谢接听。再见。"], "safe_exit", None
            elif (AVAILABLE.fullmatch(normalize_answer(text)) or guidance
                  or (self.policy.answer_implied_need and asks_for_explanation(text))):
                doc = self._find_case_doc(self.context)
                explanation, route_outcome, slots, source = self._explain(text, doc, self.context)
                if route_outcome != "rejected":
                    sentences, outcome, stage = ["请以京麦的书面审核意见为准。"], "route_unknown", None
                else:
                    yield {"kind": "stage", "reply_id": reply, "stage": "NEED_CONFIRM"}
                    state.slots, state.source = slots, source
                    reason = explanation[0].split("原因是", 1)[-1]
                    state.spoken_reason = reason.rstrip("。")
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
        elif result_confirmed(text):
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
        elif self.policy.repair_followups:
            sentences, repair = await self._repair_reply(text, state)
            outcome, stage = "pending_result", None
        else:
            explanation, _, _, _ = self._explain(text, self._find_case_doc(self.context), self.context)
            sentences = self._followup(text, explanation, state.slots)
            sentences.append("请问您确认听明白整改办法了吗？")
            outcome, stage = "pending_result", None

        for index, sentence in enumerate(sentences, start=1):
            cancel.check()
            yield {"kind": "sentence", "reply_id": reply, "text": sentence,
                   "sentence_id": index, "final": index == len(sentences)}
        if stage:
            reached, state.phase = stage
            yield {"kind": "stage", "reply_id": reply, "stage": reached}
        result = dict(state.source or {})
        if repair:
            result["followup"] = repair
        if state.writeback:
            result["writeback"] = state.writeback
        yield {"kind": "end", "reply_id": reply, "outcome": outcome, "slots": state.slots or {}, "result": result}
