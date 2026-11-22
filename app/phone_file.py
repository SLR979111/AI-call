"""Work-order DAGs. Only a grounded model decision can credit a node."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from graphlib import CycleError, TopologicalSorter
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import TaskCard

NodeId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")]
ShortText = Annotated[str, Field(min_length=1, max_length=320)]


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: NodeId
    goal: ShortText
    done_when: ShortText
    requires: list[NodeId] = Field(default_factory=list, max_length=16)
    checkpoints: list[NodeId] = Field(default_factory=list, max_length=8)


class Graph(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nodes: list[Node] = Field(min_length=2, max_length=28)
    default_leaf: NodeId

    def dependencies(self):
        return {n.id: tuple(n.requires) for n in self.nodes}

    def leaves(self):
        parents = {p for n in self.nodes for p in n.requires}
        return [n.id for n in self.nodes if n.id not in parents]

    @model_validator(mode="after")
    def consistent(self):
        deps = self.dependencies()
        if len(deps) != len(self.nodes) or self.default_leaf not in self.leaves():
            raise ValueError("需要唯一节点和有效的叶子目标")
        if any(p not in deps for parents in deps.values() for p in parents):
            raise ValueError("未知前置节点")
        try:
            tuple(TopologicalSorter(deps).static_order())
        except CycleError as error:
            raise ValueError("流程中有环") from error
        return self


class PhoneFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal["ai-call.phone.dag.v1"] = "ai-call.phone.dag.v1"
    task: TaskCard
    graph: Graph
    source: Literal["work_order", "model"] = "work_order"

    @model_validator(mode="after")
    def valid_points(self):
        ids = {p.id for p in self.task.checkpoints}
        if {p for n in self.graph.nodes for p in n.checkpoints} != ids:
            raise ValueError("图中必须包含全部工单要点，且只能引用本工单")
        return self

    def fingerprint(self):
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()[:20]


def compile_phone(task: TaskCard) -> PhoneFile:
    nodes = [Node(id="purpose", goal="让对方知道这通电话为什么与自己有关",
                  done_when="对方确认来意、说明现状，或主动提出与此工单相关的具体问题。")]
    for point in task.checkpoints:
        nodes.append(Node(id="step_" + point.id, goal=point.description, requires=["purpose"],
                          done_when="对方能确认这一项的做法，或以具体情况说明这一项已解决。", checkpoints=[point.id]))
    steps = nodes[1:]
    for step in steps:
        nodes.append(Node(id="goal_" + step.checkpoints[0], goal="解决对方单独关心的问题：" + step.goal,
                          requires=[step.id], done_when="这一项已经说清，对方表示只需处理此事或已无相关疑问。"))
    nodes.append(Node(id="goal_all", goal="对方知道这张工单接下来该怎么做",
                      requires=[s.id for s in steps], done_when="必要要点已弄清，对方确认知道下一步。"))
    return PhoneFile(task=task, graph=Graph(nodes=nodes, default_leaf="goal_all"))


PLAN_PROMPT = """从这张工单生成一张中文电话任务 DAG。只返回一个 JSON 对象。
{"nodes":[{"id":"英文id","goal":"这一步让对方弄明白的具体事情","done_when":"通话中可观察的完成证据","requires":["必要前置id"],"checkpoints":["涉及的原工单要点id"]}],"default_leaf":"一个叶子id"}
覆盖所有工单要点。用约5至12个短节点。终点是业务情境下不同的实际沟通结果，例如尚未处理与已经办理的人各自需要知道什么。用户可以从业务中途进入。已完成办理、只等结果的分支仅依赖确认对方现状，不依赖讲解怎么办理。已明确的身份可以用直接回答身份问题来确认。叶子 goal 必须描述对方最后弄明白的事。将可并行的小目标并列，只连接真实依赖，勿把互斥情形设为同一节点的共同前提。不要把识别意图、暂停通话或说再见当作所有分支的共同目标。不要虚构事实、操作能力或承诺。"""


class Prerequisites(BaseModel):
    id: NodeId
    requires: list[NodeId] = Field(max_length=16)


class DependencyReview(BaseModel):
    changes: list[Prerequisites] = Field(default_factory=list, max_length=28)


REVIEW_PROMPT = """检查已生成电话任务图的每个前置依赖，特别是每个叶子的全部祖先。用一个从中途来电的反例检验：如果来电人自己已经完成这项业务，且此刻只问之后怎么办，客服需要先教他怎样完成这项业务吗？已提交后等待结果的叶子只依赖确认身份或确认已提交的独立节点，不能依赖教人修改或重新提交的节点，也不能经其他节点间接依赖它们。只修正不必要或缺失的边，不改写节点。保留正常主路径的真正必要依赖，不要把所有路径扁平化。目标仍应是叶子。只输出 JSON：{"changes":[{"id":"待修改节点","requires":["修改后的全部直接前置节点"]}]}，无需修改时 changes 为空。"""


class Credit(BaseModel):
    id: NodeId
    quote: ShortText


class Route(BaseModel):
    leaf: NodeId
    completed: list[Credit] = Field(default_factory=list, max_length=16)
    reopen: list[Credit] = Field(default_factory=list, max_length=16)
    need: str = Field(default="", max_length=200)
    next_node: NodeId | None = None


ROUTE_PROMPT = """你为正在进行的电话选择任务图的终点并评估节点。只输出JSON
{"leaf":"有效的叶子id","completed":[{"id":"节点id","quote":"从最新用户原话逐字摘出的依据"}],"reopen":[{"id":"节点id","quote":"原话"}],"need":"用户这轮具体需要什么，一句话","next_node":"用户最需要且前置已满足的节点id"}。
先选叶子，再按 paths[所选叶子] 的顺序判断该路径所有尚未完成的前置节点，特别检查新选路径的前置。若用户说“全部改好并重新提交了”，直接引用原话完成“确认目前是否已提交”这个节点。不要根据旧路径选择节点。只返回有本轮原话证据的新增完成或撤销，之前完成的不用重报。选择能解决当前诉求的叶子；用户改变情况时可以改选。仅当对方原话结合已听到的话满足节点的 done_when 才标记完成。只说嗯、好或听过一次不足以确认理解。未听懂、纠正或提出新限制时保留当前节点，必要时撤销此前误判。要求解释全貌时先说来意，连续追问时换个角度。不要用客服本轮准备说的话作完成依据。"""


@dataclass
class Flow:
    file: PhoneFile
    leaf: str = ""
    done: dict[str, str] = field(default_factory=dict)
    need: str = ""
    event: str = ""
    heard_together: str = ""
    preferred: str | None = None
    previous_question: str = ""
    judged: bool = False

    def __post_init__(self):
        self.leaf = self.leaf or self.file.graph.default_leaf

    def accept(self, user: str, previous_question: str = ""):
        self.previous_question = previous_question
        self.need = user
        if re.search(r"整个事情|到底.{0,9}(?:讲|说)|什么事[啊呀]|来意", user):
            self.event = "需要全貌"
        elif re.search(r"没.{0,4}(?:懂|理解|明白)|不(?:懂|理解|明白)|什么(?:意思|意思啊)|多(?:解释|讲)|解释一下", user):
            self.event = "没听懂"
        elif re.search(r"然后呢|下一项|但是|不过|不是|你有告诉我", user):
            self.event = "新问题"
        else:
            self.event = ""

    def apply(self, route: Route, user: str):
        graph = self.file.graph
        if route.leaf not in graph.leaves():
            return
        self.judged = True
        self.leaf = route.leaf
        self.preferred = route.next_node
        self.need = route.need or self.need
        deps = graph.dependencies()
        revoked = {v.id for v in route.reopen if v.quote.strip() and v.quote in user}
        path = self.path()
        current = next((k for k in path if k not in self.done), self.leaf)
        approvals = {}
        for credit in route.completed:
            if credit.id not in path or not credit.quote.strip() or credit.quote not in user:
                continue
            if re.fullmatch(r"[嗯啊哦好的是对行可以，,。！!\s]+", credit.quote):
                # A yes can answer the identity/availability question just heard.
                # It cannot acknowledge other nodes or an unspoken question.
                if (credit.id != current or not self.previous_question
                        or not re.search(r"是|对|可以|方便", credit.quote)):
                    continue
            approvals[credit.id] = credit.quote
        for key in TopologicalSorter(deps).static_order():
            if key in revoked or not all(p in self.done for p in deps[key]):
                self.done.pop(key, None)
            elif key in approvals and self.event not in {"没听懂", "需要全貌"}:
                self.done[key] = approvals[key]

    def path(self, leaf=None):
        deps = self.file.graph.dependencies()
        needed = {leaf or self.leaf}
        order = list(TopologicalSorter(deps).static_order())
        for key in reversed(order):
            if key in needed:
                needed.update(deps[key])
        return [key for key in order if key in needed]

    def context(self):
        graph = self.file.graph
        path = self.path()
        nodes = {n.id: n for n in graph.nodes}
        ready = [key for key in path if key not in self.done and all(p in self.done for p in nodes[key].requires)]
        current = self.preferred if self.preferred in ready else next(iter(ready), self.leaf)
        node = nodes[current]
        return {"file": self.file.fingerprint(), "leaf": self.leaf, "final_goal": nodes[self.leaf].goal,
                "current": current, "goal": node.goal, "done_when": node.done_when, "available": ready,
                "checkpoints": node.checkpoints, "completed": list(self.done), "path": path,
                "need": self.need, "event": self.event,
                **({"repair": "先用一两句交代为什么打来、这件事与对方有什么关系以及对方要做什么。对方此刻在问来意，避免重复上一轮的文件清单和公司名。"} if self.event == "需要全貌" else
                   {"repair": "用白话换一个例子解释卡住的概念。对方听懂前保留这个节点。"} if self.event == "没听懂" else {}), "finished": self.leaf in self.done,
                **({"heard_together": self.heard_together} if self.heard_together else {})}

    def candidates(self, options):
        if self.event:
            return []
        scope = self.context()
        points = scope["checkpoints"]
        return [c for c in options if not c.checkpoint or not points or c.checkpoint in points]


async def model_json(provider, prompt, data, schema, tokens):
    messages = [{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(data, ensure_ascii=False)}]
    kwargs = {"schema": schema.model_json_schema()} if getattr(provider, "is_local", False) else {}
    text = "".join([part async for part in provider.chat(messages, max_tokens=tokens, **kwargs)]).strip()
    text = re.sub(r"^\x60{3}(?:json)?\s*|\s*\x60{3}$", "", text)
    return schema.model_validate_json(text)
