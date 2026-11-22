"""A bounded merchant simulator that reacts to exactly what was said."""
from __future__ import annotations

import re

from .pop_closure import FOLLOWUP_EVIDENCE, RECORD, CLOSED_SAFELY

IDENTITY = "pending_identity"
NEED = "pending_need"
OFFER = "pending_result"
QUESTIONS = {
    "action": "先说明这笔具体让我补或者改什么？",
    "channel": "那书面意见从哪里能找到？",
    "review": "还有一件事，重交之后要重新审核吗？",
    "restate": "我只听见一部分，能把退回原因和处理办法一起再说一遍吗？",
}
PROFILES = {
    "dev": (
        ["嗯，是我负责店里这笔。", "对，我是申请负责人。", "这块归我管，我在跟。"],
        ["现在能讲，我稍微有点时间。", "可以，您说。", "嗯，我这阵不忙，接着说吧。"],
        ["我听懂了，我会按这笔的要求处理。", "好，知道要怎么改了。", "清楚了，等下回后台办。"],
    ),
    "validation": (
        ["我是这里跟资质的人，正是我。", "是的，您找的负责人就是我。", "对，店里的申请我来办。"],
        ["这会刚好方便，您接着说。", "方便，您讲吧。", "好，我可以先听。"],
        ["我已经清楚要怎么处理了。", "听明白了，我回原申请去办。", "好，下一步我都知道了。"],
    ),
    "test": (
        ["对，我就是店里的经办人。", "是我本人，店铺的申请归我做。", "对，这笔是我在办。"],
        ["正好空下来了，你说。", "行，您讲吧。", "我现在能听，继续吧。"],
        ["听清了，我回去按申请上的说明改。", "这下知道怎么改了，我稍后去弄。", "嗯，整改怎么做我已经明白。"],
    ),
}
RETRIES = {"dev": "可以，请重试一次。", "validation": "好，您重新记录一下吧。",
           "test": "麻烦再试一下，谢谢。"}


def text_of(turn):
    return "".join(turn["sentences"])


def normalize(text):
    return re.sub(r"[\W_]+", "", text)


def answers(topics, text, cause, remedy):
    return {topic for topic in topics if
            (topic == "action" and remedy and normalize(remedy) in normalize(text))
            or (topic == "restate" and cause and remedy and normalize(cause) in normalize(text)
                and normalize(remedy) in normalize(text))
            or (topic in FOLLOWUP_EVIDENCE and re.search(FOLLOWUP_EVIDENCE[topic], text))}


class Merchant:
    def __init__(self, case):
        self.case = case
        self.result = {
            "failures": [], "questions": 0, "first_answer_complete": None,
            "answered": [], "retries": [], "reported_clear": False,
        }

    def steps(self, turns):
        case, result = self.case, self.result
        kind = case["kind"]
        if kind == "wrong":
            yield [case["question"], CLOSED_SAFELY]
            return
        profile = PROFILES[case["split"]]
        identity, available, confirm = (items[case["profile"]] for items in profile)
        # The evaluated expected stage is recorded separately from the
        # simulator's routing, including whether a legitimate reprompt occurred.
        yield [identity, (IDENTITY, NEED)]
        if turns[-1]["outcome"] == IDENTITY:
            result["retries"].append("identity")
            yield ["是的，我是这笔申请的负责人。", NEED]
        if turns[-1]["outcome"] != NEED:
            result["failures"].append("could_not_verify")
            return
        yield [available, (NEED, OFFER)]
        if turns[-1]["outcome"] == NEED:
            result["retries"].append("availability")
            yield ["我现在方便，请说。", OFFER]
        if turns[-1]["outcome"] != OFFER:
            result["failures"].append("could_not_start_offer")
            return
        if kind == "stop":
            yield [case["question"], CLOSED_SAFELY]
            return

        offer = turns[-1]["result"]
        remedy = offer.get("remediation", "")
        cause = next((line.removeprefix("审核问题是") for line in turns[-1]["sentences"]
                      if line.startswith("审核问题是")), "")
        topics = case["topics"]
        yield [case["question"], OFFER]
        result["questions"] += 1
        if turns[-1]["outcome"] != OFFER:
            result["failures"].append("left_unanswered")
            return
        covered = answers(topics, text_of(turns[-1]), cause, remedy)
        if topics:
            result["first_answer_complete"] = covered == set(topics)
            if missing := [topic for topic in topics if topic not in covered]:
                result["retries"].append("unanswered_question")
                yield [" ".join(QUESTIONS[topic] for topic in missing), OFFER]
                result["questions"] += 1
                covered |= answers(missing, text_of(turns[-1]), cause, remedy)
            result["answered"] = sorted(covered)
            if covered != set(topics):
                result["failures"].append("left_unanswered")
                if turns[-1]["outcome"] == OFFER:
                    yield ["我还是没弄清楚，我先挂了，再见。", CLOSED_SAFELY]
                return

        result["reported_clear"] = True
        if kind == "fault":
            yield [confirm, "writeback_failed", "failed"]
            retry = (RETRIES[case["split"]] if "再试一次" in text_of(turns[-1])
                     else "我已经听明白整改办法了，麻烦再试一次记录。")
            yield [retry, RECORD, "returned"]
            return
        yield [confirm, (OFFER, RECORD), "if_returned"]
        if turns[-1]["outcome"] == OFFER:
            result["retries"].append("confirmation")
            yield ["我确认已经听明白整改办法了。", RECORD, "returned"]
