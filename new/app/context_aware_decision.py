"""Conservative lexical matching for predicted replies in a live conversation."""

from __future__ import annotations

import re
from typing import Literal

from .forecast import compatible_short_answer, match_example, spoken_form
from .models import Candidate


# Each intent has separate surface forms in speech and in forecast conditions.
# The positive part of a forecast must explicitly name the same intent.
INTENTS = {
    "submitted": (r"(?:已经|已|点了|重新|再次|完成了?)?提交了|重新提交|点了提交", r"(?<!没)(?<!未)(?<!点)提交|补交"),
    "saved": (r"(?:只|只是|仅|已经|已)?保存了?|还没提交", r"保存|没提交"),
    "prepared": (r"(?:已|已经)?准备(?:好|了)|开好了|截好了|补好了", r"准备|开好|截好|补好"),
    "direct": (r"直接(?:授权|给我|给的)|品牌方(?:直接)?(?:授权|给的)", r"直接授权|直接给|直授"),
    "reseller": (r"经销商(?:转授|授权|给的)|远杉(?:转授|给的)", r"经销商|转授|远杉"),
    "clear": (r"(?:都)?对得上|(?:都)?一致|(?:能)?看清|清楚了?", r"对得上|一致|看清|清楚"),
    "unclear": (r"对不上|不一致|看不清|不清楚", r"对不上|不一致|看不清|不清楚"),
    "ack": (r"明白了?|懂了|清楚了?|了解了?", r"明白|理解|懂了|清楚|了解"),
    "owner": (r"(?:我|本人)?是(?:的)?(?:负责人|负责这块|负责这个)", r"负责|负责人"),
}

# Signals that a forecast's positive clause demands another, distinct fact.
REQUIREMENTS = {
    "submitted": r"提交|补交", "saved": r"保存|没提交", "prepared": r"准备|开好|截好|补好",
    "direct": r"直授|直接授权|直接给", "reseller": r"经销商|转授",
    "clear": r"对得上|一致|看清|清楚", "unclear": r"对不上|不一致|看不清|不清楚",
    "ack": r"明白|理解|懂了|了解", "owner": r"负责|负责人",
}

EXTRA = re.compile(r"[?？]|为什么|怎么|如何|哪里|哪儿|多少|能不能|可不可以|但是|不过|可是|然而|另外|还有|顺便|其实|改口|不方便|忙|拒绝|算了|不用|不要|不需要|催|加急")
NEGATION = re.compile(r"不是|不对|没有|没|不行|不可以|不清楚|不一致|对不上|看不清")


class ContextAwareDecisionEngine:
    """Only choose a forecast when its condition has unambiguous lexical evidence."""

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"[\s，。；：？！,.!?、‘’“”'\"（）()【】]", "", text).lower()

    @staticmethod
    def _positive_condition(when: str) -> str:
        # Everything after these delimiters is an exclusion, qualification or
        # safety note, not evidence that the user met the positive condition.
        return re.split(r"且|但|；|;|。|，没有|,没有", when, maxsplit=1)[0]

    @staticmethod
    def _user_intents(text: str) -> set[str]:
        found = {name for name, (speech, _) in INTENTS.items() if re.search(speech, text)}
        # Do not mistake "还没提交" for a submitted application, nor
        # "对不上" for an affirmative match.
        if re.search(r"(?:没|未|尚未|还没|没有|只是保存了.{0,4}没)提交", text):
            found.discard("submitted")
        if "unclear" in found:
            found.discard("clear")
        return found

    @staticmethod
    def _condition_intents(text: str) -> set[str]:
        found = {name for name, (_, condition) in INTENTS.items() if re.search(condition, text)}
        if re.search(r"没(?:点)?提交|未提交|还没(?:点)?提交", text):
            found.discard("submitted")
        if re.search(r"不是|不负责", text):
            found.discard("owner")
        if "unclear" in found:
            found.discard("clear")
        if re.search(r"(?:已|已经|重新|点了)提交|提交了", text):
            found.discard("saved")
        return found

    def _rank_short_reply(self, text: str, candidates: list[Candidate], previous: str) -> Candidate | None:
        """Rank common spoken answers against forecast branches, not list order."""
        if len(text) > 24 or re.search(r"但是|不过|另外|还有|不是我的", text):
            return None
        topics = [
            (r"直接.*(?:给|授权)", r"直接授权|直接给|直授", r"仍|经销商给|转授"),
            (r"经销商", r"经销商|转授", r"已改成.*直接"),
            (r"对得上", r"对得上", r"对不上|有问题"),
            (r"开好了", r"开好|拿到", r"没开好|还没"),
            (r"准备了|准备好了", r"已准备|准备好", r"没准备|还没|未准备"),
            (r"改过名|更名", r"改过名|更名", r"填错字"),
            (r"已经改了", r"已改|改成", r"还没改"),
            (r"没有了|再见", r"没有其他问题|结束|再见|都清楚|都已明白", r"仍有疑问"),
            (r"^(?:有的?|有这份授权)$", r"手上有|已有|能拿到|能补", r"没有|还没|看不清"),
        ]
        scored = []
        short_yes = bool(re.fullmatch(r"(?:是的?|对的?|好的?|可以|明白了|嗯+)+(?:谢谢)?", text))
        ordinal = bool(re.fullmatch(r"(?:先听)?第一(?:项|处|段)(?:吧)?", text))
        for candidate in candidates:
            condition = self._positive_condition(candidate.when)
            score = 0
            for speech, positive, negative in topics:
                if re.search(speech, text) and re.search(positive, condition) and not re.search(negative, condition):
                    score += 6
            if ordinal and '授权' in previous and re.search(r"选择先听授权", condition):
                score += 6
            if short_yes:
                # Evaluate disqualifiers before truncating at 但: it can introduce
                # a positive busy/refusal branch, not just an exclusion.
                branch = re.split(r"且|；|;", candidate.when)[0]
                if re.search(r"但.*(?:忙|不方便)|不是负责人|拒绝|打错", branch):
                    continue
                if re.search(r"负责.*吗", previous) and '负责' in condition:
                    score += 6
                    if re.search(r"只问|只笼统|某一项", branch):
                        score -= 2
                if re.search(r"知道|明白|会去|承接|继续", condition):
                    score += 3
                if re.search(r"能.*吗", previous) and re.search(r"能重新|能看清|会去", condition):
                    score += 4
            if score:
                scored.append((score, candidate))
        scored.sort(key=lambda item: item[0], reverse=True)
        if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            return scored[0][1]
        return None

    def should_reuse(
        self, user_input: str, candidates: list[Candidate], assistant_last_text: str = ""
    ) -> tuple[Literal["reuse", "say", "llm"], Candidate | None]:
        if not candidates or not user_input.strip():
            return "llm", None

        key = spoken_form(user_input)
        candidates = [c for c in candidates if compatible_short_answer(key, assistant_last_text, c)]
        example = match_example(user_input, candidates, assistant_last_text)
        if example is not None:
            return "reuse", example
        # Keep an unpredicted question or correction out of the lexical rules.
        if EXTRA.search(user_input):
            return "llm", None
        utterance = self._normalize(key)
        # Parallel forecasts often repeat the same answer intent. Exact short
        # answers can select among equivalent branches without a model roundtrip.
        explicit = (
            (r"(?:已经|已)?(?:重新)?提交了", r"已经.*提交|已.*提交", r"只是保存|没提交|不清楚"),
            (r"能看清", r"能看清", r"看不清|模糊|只有注册证"),
            (r"清楚了?", r"说清楚|已知道|表示清楚", r"不清楚|没听懂"),
            (r"好的?(?:谢谢)?", r"明白或同意继续听|纯承接|简单.*(?:肯定|承接)|用户(?:只)?(?:说|回答|表示)[‘’“”\"']?好的", r"并问|追问|已提交"),
        )
        for speech, positive, negative in explicit:
            if not re.fullmatch(speech, utterance):
                continue
            equivalent = [c for c in candidates
                          if re.search(positive, self._positive_condition(c.when))
                          and not re.search(negative, self._positive_condition(c.when))]
            if equivalent:
                # Shorter audio reaches the next listening opportunity sooner;
                # stable text/id ordering makes completion order irrelevant.
                return "reuse", min(equivalent, key=lambda c: (len(c.reply.text), c.reply.text, c.id))
        selected = self._rank_short_reply(utterance, candidates, assistant_last_text)
        if selected is not None:
            return "reuse", selected
        # Latency-first routing for common short replies: prefer the forecast
        # that continues the current topic, even when its wording is stricter
        # than the acknowledgement itself. Never choose by candidate position.
        if re.fullmatch(r"(?:好的?|嗯+|行|可以|收到|知道了|明白了|是的?|对的?)+(?:谢谢)?", utterance):
            scored = []
            for candidate in candidates:
                positive = self._positive_condition(candidate.when)
                if re.search(r"不是|不负责|打错|忙|不方便|拒绝|没提交|只是保存|仍有疑问", positive):
                    continue
                score = 0
                if re.search(r"知道|明白|理解|了解|会去|继续|确认", positive):
                    score += 2
                if re.search(r"负责", positive) and re.search(r"负责.*吗", assistant_last_text):
                    score += 4
                if re.search(r"提交", positive) and re.search(r"提交.*吗", assistant_last_text):
                    score += 3
                if score:
                    scored.append((score, candidate))
            scored.sort(key=lambda item: item[0], reverse=True)
            if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
                return "reuse", scored[0][1]
        # A second question, correction or changed preference needs the full
        # dialogue context. Keep forecasts available to the LLM for evaluation.
        if EXTRA.search(user_input) or len(utterance) > 24:
            return "llm", None

        affirmation = bool(re.fullmatch(r"(?:是的?|对的?|没错|嗯+)+", utterance))
        acknowledgement = bool(re.fullmatch(r"(?:明白了?|懂了|了解了?|清楚了?)+", utterance))
        if NEGATION.search(utterance) and not (self._user_intents(utterance) & {"saved", "unclear"}):
            return "llm", None
        if affirmation and (
            not re.search(r"(?:吗|么|吧)[?？]?$", assistant_last_text.strip())
            or re.search(r"还是|或者|不|没|哪|怎么", assistant_last_text)
        ):
            return "llm", None

        # Require the entire short answer to be understood. A keyword inside
        # "提交了还能撤回吗" or "保存了但文件错了" is not sufficient.
        forms = "|".join(speech for speech, _ in INTENTS.values())
        if not affirmation and not re.fullmatch(
            rf"(?:我)?(?:已经|已)?(?:{forms})(?:的|了)?(?:谢谢)?", utterance
        ):
            return "llm", None
        user_intents = self._user_intents(utterance)
        if acknowledgement:
            user_intents = {"ack"}
        matches = []
        for candidate in candidates:
            # Do not discard additional positive requirements after 且/但.
            if re.search(r"(?:且|但)(?!没有|没|尚未|未|不能|不)", candidate.when):
                continue
            condition = self._positive_condition(candidate.when)
            # "是负责人，并直接问退回原因" requires two acts; a bare
            # affirmation to the identity question supplies only the first.
            if re.search(r"直接问|追问|询问|并.*问|忙|不方便|拒绝|不是|不负责", condition):
                continue
            condition_intents = self._condition_intents(condition)
            if "direct" in condition_intents and "prepared" in condition_intents and "direct" not in user_intents:
                if "直接授权" in assistant_last_text and "文件" in assistant_last_text:
                    condition_intents.remove("direct")
            if affirmation:
                # A bare yes only confirms the immediately preceding question.
                # It never supplies additional facts required by a forecast.
                question = self._normalize(assistant_last_text)
                relevant = {name for name in condition_intents
                            if re.search(REQUIREMENTS[name], question) or
                            (name == "owner" and re.search(r"负责(?:这块|这个|这次).*吗", question))}
                if not relevant or relevant != condition_intents:
                    continue
                if "saved" in relevant or "unclear" in relevant:
                    continue
            elif not user_intents or not condition_intents or not user_intents <= condition_intents:
                continue
            elif condition_intents - user_intents:
                continue
            if re.search(r"(?:没|未|不|尚未|还没).{0,3}(?:" + "|".join(REQUIREMENTS[name] for name in condition_intents) + r")", utterance) and not (user_intents & {"saved", "unclear"}):
                continue
            matches.append(candidate)
        # Overlapping or underspecified forecasts are ambiguous; don't guess by id/order.
        if len(matches) == 1:
            return "reuse", matches[0]
        return "llm", None
