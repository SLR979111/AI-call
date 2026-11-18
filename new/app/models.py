"""Small task state. Delivery and understanding are deliberately separate."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Checkpoint(BaseModel):
    id: str = Field(min_length=1, max_length=32, pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(min_length=1, max_length=300)


class TaskCard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=80)
    goal: str = Field(min_length=1, max_length=1000)
    facts: str = Field(max_length=5000)
    checkpoints: list[Checkpoint] = Field(min_length=1, max_length=8)
    opening: str = Field(min_length=1, max_length=180)

    @model_validator(mode="after")
    def unique_points(self):
        if len({point.id for point in self.checkpoints}) != len(self.checkpoints):
            raise ValueError("checkpoint ids must be unique")
        return self


# Reject unmistakable internal narration before it can reach history, UI or TTS.
# Ordinary explanations (including why a document was rejected) remain allowed.
INTERNAL_SPEECH = re.compile(
    r"<\s*/?\s*(?:think(?:ing)?|analysis|reasoning)\b|<\|(?:analysis|reasoning)\|>"
    r"|任务卡|检查点|内部(?:判断|策略|推理|思考)"
    r"|(?:^|[\n。！？])\s*[【\[]?(?:内部分析|思考过程|推理过程|思维链|内心独白|分析|思考|策略)\s*[:：】\]]"
    r"|(?:^|[\n。！？])\s*(?:用户(?:现在|目前|已经|表示|想要)|我(?:应该|需要)先(?:判断|分析|询问)|接下来我(?:应该|需要))",
    re.IGNORECASE,
)


class Say(BaseModel):
    text: str = Field(min_length=1, max_length=180)
    covers: list[str] = Field(default_factory=list, max_length=8)
    question: str = Field(default="", max_length=160)

    @field_validator("text")
    @classmethod
    def spoken_content_only(cls, text: str) -> str:
        if INTERNAL_SPEECH.search(text):
            raise ValueError("internal narration is not spoken content")
        return text


class Observation(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    value: str = Field(min_length=1, max_length=200)
    quote: str = Field(min_length=1, max_length=200)


class Update(BaseModel):
    facts: list[Observation] = Field(default_factory=list, max_length=10)
    questions: list[str] = Field(default_factory=list, max_length=6)
    confirmation: str = Field(default="", max_length=200)
    disposition: Literal["active", "complete", "paused", "declined"] = "active"
    disposition_quote: str = Field(default="", max_length=200)


class Candidate(BaseModel):
    id: str = Field(min_length=1, max_length=32)
    when: str = Field(min_length=1, max_length=300)
    examples: list[Annotated[str, Field(min_length=1, max_length=90)]] = Field(default_factory=list, max_length=2)
    reply: Say


def farewell_only(text: str) -> bool:
    text = re.sub(r"[\s，,。.!！?？；;]", "", text).lower()
    return bool(re.fullmatch(r"(?:好的?|嗯+|呃|哦|那|就|先这样|再见|拜拜|拜|我?先?挂了|谢谢您?|ok|吧|啊|呀|了)+", text))


def wants_to_end(text: str) -> bool:
    """Recognize direct call controls, not a mention of saying goodbye."""
    if re.search(r"(?:别|不要|不用|先不|不想|不能)挂|(?:别|不要)结束|还有.{0,5}问题|还没说完"
                 r"|什么意思|怎么理解|(?:你|您)(?:刚才?|刚刚)?说(?:的)?", text):
        return False
    if re.fullmatch(r"(?:好的?|嗯|明白了|知道了|谢谢您?)?[，,。\s]*(?:那就?|就)?先这样[吧了]?[，,。\s]*"
                    r"(?:再见|拜拜)[。！!\s]*", text.strip()):
        return True
    return bool(re.search(
        r"(?:别|不要|不用|不必)(?:再|继续)?(?:说|讲|聊)了?"
        r"|(?:不想|不愿意|不方便)(?:再|继续)?(?:说|聊)"
        r"|(?:我先|我得|我要|那我|我就)(?:挂了|挂电话|挂断)"
        r"|(?:^|[，,。！!])\s*先挂了[，,。\s]*(?:谢谢您?|再见|拜拜)?[。！!\s]*$"
        r"|(?:请|麻烦)(?:您|你)?(?:直接|现在|就)?挂(?:断|掉)?(?:电话|通话)?"
        r"|(?:直接|现在|就)挂(?:断|掉)?(?:电话|通话)"
        r"|怎么(?:还)?不挂(?:断)?(?:电话|通话)"
        r"|(?:^|[，,。！!])\s*(?:挂断|挂电话|结束通话)(?:吧|了|呀|啊|好吗)?[。.!！?？\s]*$",
        text,
    ))


def ends_with_goodbye(text: str) -> bool:
    """An actual closing clause, not a question about the word 'goodbye'."""
    return bool(re.search(
        r"(?:^|[，,。！!\s])(?:那就|那|好|好的|谢谢您?|先这样)?[，,\s]*"
        r"(?:再见|拜拜)(?:吧|啊|呀|了|啦|喽|咯)*[。.!！~～\s]*$", text))


@dataclass
class Memory:
    card: TaskCard
    facts: dict[str, dict] = field(default_factory=dict)
    questions: list[str] = field(default_factory=list)
    delivered: set[str] = field(default_factory=set)
    last_question: str = ""
    status: str = "active"
    history: list[dict] = field(default_factory=list)

    def delivered_covers(self, line: Say):
        covers = list(line.covers)
        if line.question or re.search(r"[？?]", line.text):
            return covers
        spoken = line.text.strip().rstrip("。！!.")
        for point in self.card.checkpoints:
            statement = re.sub(r"^(?:说明|告知|告诉对方)", "", point.description.strip()).rstrip("。！!.")
            if len(statement) >= 4 and statement == spoken and point.id not in covers:
                covers.append(point.id)
        return covers

    def requested_replay(self):
        if not self.history or self.history[-1]["role"] != "user":
            return []
        request = re.sub(r"[\s，,。.!！?？]", "", self.history[-1]["text"])
        # Replay only an explicit, standalone request. For a correction, a
        # follow-up or an interrupted unit the regular response needs context.
        if not re.fullmatch(r"(?:不好意思|抱歉)?(?:我?(?:刚才|刚刚)?没听清(?:楚)?)?"
                            r"(?:请|麻烦)?(?:您|你)?(?:再说|再讲|重说|重复)(?:一遍|一下)(?:吧|好吗)?", request):
            return []
        lines = []
        for entry in reversed(self.history[:-1]):
            if entry["role"] == "user":
                break
            if entry.get("filler") or entry.get("bridge"):
                continue
            if entry.get("delivery") != "complete" or entry.get("remaining_text"):
                return []
            text = entry["text"]
            lines.append(Say(text=text, question=self.last_question if self.last_question in text else ""))
        return list(reversed(lines))

    def interrupted_position(self) -> tuple[str, str] | None:
        """Answer an explicit cutoff question from playback, preserving any follow-up."""
        if not self.history or self.history[-1]["role"] != "user":
            return None
        user = self.history[-1]["text"]
        match = re.match(r"^(?:(?:请问|刚才|刚刚|你|您|我打断[你您]?(?:的时候|时))[，,\s]*)*"
                         r"(?:讲|说)到(?:哪[里儿]?|什么地方)[了啦啊呀呢吗]*"
                         r"(?=[，,。.!！?？\s]|$)", user)
        if not match:
            return None
        previous = []
        for entry in reversed(self.history[:-1]):
            if entry["role"] == "user":
                break
            previous.append(entry)
        if not any("remaining_text" in entry for entry in previous):
            return None
        spoken = next((entry["text"] for entry in previous if entry.get("text")
                       and not (entry.get("filler") or entry.get("bridge"))), "")
        tail = spoken.rstrip("，,。.!！?？；; ").strip()[-16:]
        if tail:
            recap = f"刚才大约说到“{tail}”。"
        elif any(entry.get("played_ms") for entry in previous):
            recap = "刚才只开了个头。"
        else:
            recap = "刚才那句话还没说出来。"
        return recap, user[match.end():].lstrip("，,。.!！?？ ")

    def ready_to_say_goodbye(self, user: str, last_reply: Say) -> bool:
        # Even a valid confirmation can be followed by a new request to keep talking.
        return (self.status == "complete" and not self.questions
                and not re.search(r"[？?]|(?:别|不要|先不)挂|还(?:想|要)问|还有(?:个|一个)?问题", user)
                and not last_reply.question and not last_reply.text.rstrip().endswith(("？", "?")))

    def closing_reason(self, user: str) -> str:
        # Ending a call and completing its business objective are separate.
        # In particular, a second goodbye must not depend on checkpoint credit.
        if wants_to_end(user):
            return "requested"
        if not farewell_only(user):
            return ""
        index = next((i for i in range(len(self.history) - 1, -1, -1)
                      if self.history[i]["role"] == "user"), len(self.history))
        previous = self.history[:index]
        assistant = next((entry for entry in reversed(previous)
                          if entry["role"] == "assistant" and entry.get("delivery") == "complete"), {})
        if re.search(r"再见|拜拜", assistant.get("text", "")):
            return "goodbye_acknowledged"
        has_goodbye = bool(re.search(r"再见|拜拜|先这样|挂了", user))
        if not has_goodbye:
            return ""
        if self.status == "complete":
            return "completed"
        previous_user = next((entry["text"] for entry in reversed(previous) if entry["role"] == "user"), "")
        if farewell_only(previous_user) and re.search(r"再见|拜拜|先这样|挂了", previous_user):
            return "repeated_goodbye"
        return ""

    def view(self) -> dict:
        return {
            "facts": {name: dict(fact) for name, fact in self.facts.items()},
            "questions": list(self.questions),
            "delivered": sorted(self.delivered),
            "remaining": [p.id for p in self.card.checkpoints if p.id not in self.delivered],
            "last_question": self.last_question,
            "status": self.status,
        }

    def apply(self, update: Update, user: str, turn: int, delivered_before: set[str]) -> None:
        # A generated forecast can never call this method. Only the current
        # response's observations, grounded in this user's words, may commit.
        for fact in update.facts:
            if fact.quote in user:
                self.facts[fact.name] = {"value": fact.value, "quote": fact.quote, "turn": turn}
        already_complete = self.status == "complete"
        self.questions = update.questions
        self.status = "active"
        if update.disposition in {"paused", "declined"}:
            if (update.disposition_quote and update.disposition_quote in user
                    and not farewell_only(update.disposition_quote)):
                self.status = update.disposition
        elif update.disposition == "complete":
            required = {p.id for p in self.card.checkpoints}
            quote = update.confirmation.strip()
            # Confirmation must refer to information already delivered before
            # this input, not to material we are about to say in this response.
            confirmed = (quote in user and len(quote) >= 2
                         and quote not in {"嗯嗯", "好的", "是的", "可以"}
                         and not farewell_only(quote))
            if (required <= delivered_before and (confirmed or already_complete)
                    and not self.questions):
                self.status = "complete"


DEFAULT_CARD = TaskCard(
    title="资料补正沟通",
    goal="说明资料需要补充的原因和下一步操作，解决对方的问题，并确认对方理解。对方忙或拒绝时尊重其意愿。",
    facts="这是一通演示对话。资料缺少商标当前状态的截图。需要补充这张截图，再在原提交入口重新提交。补齐材料不代表审核一定通过，结果以实际审核为准。没有任何查询、登记、转接或预约功能。",
    checkpoints=[
        Checkpoint(id="reason", description="说明缺少商标当前状态截图"),
        Checkpoint(id="next_step", description="说明补上截图后，在原提交入口重新提交"),
    ],
    opening="您好，我是资料沟通助手，想跟您说明一项需要补充的资料。现在方便说两句吗？",
)
