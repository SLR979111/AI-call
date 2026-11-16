"""Small task state. Delivery and understanding are deliberately separate."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Literal

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
    reply: Say


@dataclass
class Memory:
    card: TaskCard
    facts: dict[str, dict] = field(default_factory=dict)
    questions: list[str] = field(default_factory=list)
    delivered: set[str] = field(default_factory=set)
    last_question: str = ""
    status: str = "active"
    history: list[dict] = field(default_factory=list)

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
        self.questions = update.questions
        self.status = "active"
        if update.disposition in {"paused", "declined"}:
            if update.disposition_quote and update.disposition_quote in user:
                self.status = update.disposition
        elif update.disposition == "complete":
            required = {p.id for p in self.card.checkpoints}
            quote = update.confirmation.strip()
            # Confirmation must refer to information already delivered before
            # this input, not to material we are about to say in this response.
            if (required <= delivered_before and quote in user and len(quote) >= 2
                    and quote not in {"嗯嗯", "好的", "是的", "可以"}
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
