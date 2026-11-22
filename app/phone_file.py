"""Work-order DAGs with transport-driven entry and model-judged business goals."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from graphlib import CycleError, TopologicalSorter
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import CallPlaybook, Checkpoint, TaskCard

NodeId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")]
ShortText = Annotated[str, Field(min_length=1, max_length=320)]


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: NodeId
    kind: Literal["business", "root", "greeting"] = "business"
    goal: ShortText
    done_when: ShortText
    action: str = Field(default="", max_length=320)
    requires: list[NodeId] = Field(default_factory=list, max_length=16)
    checkpoints: list[NodeId] = Field(default_factory=list, max_length=8)


class Graph(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nodes: list[Node] = Field(min_length=2, max_length=30)
    default_leaf: NodeId

    def with_entry(self, opening: str) -> Graph:
        business = [n for n in self.nodes if n.kind == "business"]
        if not 2 <= len(business) <= 28:
            raise ValueError("工单需要 2 到 28 个业务节点")
        entry = {n.kind: n for n in self.nodes if n.kind != "business"}
        if entry and (set(entry) != {"root", "greeting"} or len(self.nodes) != len(business) + 2
                      or entry["root"].requires or entry["greeting"].requires != [entry["root"].id]
                      or any(n.checkpoints for n in entry.values())):
            raise ValueError("通话入口需要一个根节点和一个问候节点")
        used = {n.id for n in self.nodes}

        def entry_id(kind, name):
            if kind in entry:
                return entry[kind].id
            candidate = name
            index = 1
            while candidate in used:
                candidate = f"{name}_{index}"
                index += 1
            return candidate

        root = Node(id=entry_id("root", "call_start"), kind="root", goal="通话开始",
                    action="接通后进入开场。", done_when="通话已接通。")
        greeting = Node(id=entry_id("greeting", "greeting"), kind="greeting", goal="问候与自我介绍",
                        action=opening, done_when="开场白已播完，或已在文字通话中完整显示。", requires=[root.id])
        nodes = [root, greeting]
        for node in business:
            parents = [p for p in node.requires if p not in {root.id, greeting.id}]
            nodes.append(node.model_copy(update={"requires": parents or [greeting.id]}))
        return Graph(nodes=nodes, default_leaf=self.default_leaf)

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


class BasicResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    opening: str = Field(min_length=1, max_length=180)
    guide: str = Field(min_length=1, max_length=800)


class CompiledMaterials(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    guide: str = Field(min_length=1, max_length=800)
    background: str = Field(max_length=2400)
    graph: Graph

    @field_validator("graph", mode="before")
    @classmethod
    def select_leaf(cls, value):
        if not isinstance(value, dict) or not isinstance(value.get("nodes"), list):
            return value
        nodes = [Node.model_validate(node) for node in value["nodes"]]
        by_id = {node.id: node for node in nodes}
        parents = {p for node in nodes for p in node.requires}
        default = value.get("default_leaf")
        if isinstance(default, str) and default in parents and default in by_id:
            # A generated default can refer to a valid branching node. Keep its
            # route by selecting the first declared leaf reachable from it.
            reachable = {default}
            for key in TopologicalSorter({n.id: n.requires for n in nodes}).static_order():
                if key in by_id and reachable.intersection(by_id[key].requires):
                    reachable.add(key)
            default = next((n.id for n in nodes if n.id in reachable and n.id not in parents), default)
        return {**value, "nodes": nodes, "default_leaf": default}


def extract_background(text: str) -> str:
    # Offline migration: excerpt complete sentences from each section. The
    # model compiler can distinguish guidance from facts more precisely.
    if len(text) <= 2400:
        return text
    sections = [s.strip() for s in text.splitlines() if s.strip()]
    budget = 2300 // max(len(sections), 1)
    parts = []
    for section in sections:
        pieces = re.findall(r"[^。！？；]+[。！？；]?", section)
        selected = ""
        for piece in pieces:
            if selected and len(selected + piece) > budget:
                break
            selected += piece
        if selected and len("\n".join([*parts, selected])) <= 2400:
            parts.append(selected)
    return "\n".join(parts)


class PhoneFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal["ai-call.phone.dag.v2"] = "ai-call.phone.dag.v2"
    title: str = Field(min_length=1, max_length=80)
    basic_response: BasicResponse
    background: str = Field(max_length=2400)
    checkpoints: list[Checkpoint] = Field(min_length=1, max_length=8)
    graph: Graph
    source: Literal["work_order", "model"] = "work_order"

    @model_validator(mode="before")
    @classmethod
    def upgrade_v1(cls, data):
        if isinstance(data, dict) and data.get("format") == "ai-call.phone.dag.v1":
            card = TaskCard.model_validate(data.get("task"))
            return dict(format="ai-call.phone.dag.v2", title=card.title,
                        basic_response=BasicResponse(opening=card.opening, guide=DEFAULT_GUIDE),
                        background=extract_background(card.facts), checkpoints=card.checkpoints,
                        graph=data.get("graph"), source=data.get("source", "work_order"))
        return data

    @model_validator(mode="after")
    def valid_points(self):
        self.graph = self.graph.with_entry(self.basic_response.opening)
        ids = {p.id for p in self.checkpoints}
        if len(ids) != len(self.checkpoints) or {p for n in self.graph.nodes for p in n.checkpoints} != ids:
            raise ValueError("图中必须包含全部工单要点，且只能引用本工单")
        return self

    @property
    def task(self):
        # A compatibility view for the playback, ticket and prewarmer APIs.
        goal = next(n.goal for n in self.graph.nodes if n.id == self.graph.default_leaf)
        return TaskCard(title=self.title, goal=goal, opening=self.basic_response.opening,
                        facts=self.background, checkpoints=self.checkpoints)

    def briefing(self):
        return {"basic_response": self.basic_response.model_dump(), "background": self.background,
                "graph": self.graph.model_dump()}

    def with_opening(self, opening: str):
        if not opening or opening == self.basic_response.opening:
            return self
        basic = self.basic_response.model_copy(update={"opening": opening})
        return self.model_copy(update={"basic_response": basic, "graph": self.graph.with_entry(opening)})

    def fingerprint(self):
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()[:20]


DEFAULT_GUIDE = "先直接回答对方这轮的问题，再推进一个小目标。只引用已知事实；区分已发生和假设，对方没有听懂时换个角度，尊重停止意愿。"


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
    return PhoneFile(title=task.title, checkpoints=task.checkpoints, background=extract_background(task.facts),
                     basic_response=BasicResponse(opening=task.opening, guide=DEFAULT_GUIDE),
                     graph=Graph(nodes=nodes, default_leaf="goal_all"))


PLAN_PROMPT = """把原始工单编译成独立可执行的电话资料。回复客服永远看不到原工单。只返回 JSON，三个字段为 guide（用约百字说明客服身份、应答方式和能力边界）、background（2400字以内，保留名称、数字、实际记录、条件及例外，区分真实现状与假设；提炼业务依据，删除原稿的流程和话术指令）、graph。graph 格式如下。
{"nodes":[{"id":"英文id","goal":"这一步让对方弄明白的具体事情","done_when":"通话中可观察的完成证据","action":"本节点具体怎样解释、问什么或提供什么行动，不写空泛建议","requires":["必要前置id"],"checkpoints":["涉及的原工单要点id"]}],"default_leaf":"一个叶子id"}
只生成约5至12个业务节点，系统统一添加通话开始和开场白。覆盖所有工单要点。业务情境和办理对象以原工单为准，示例只用来解释结构。每个节点写清一个具体动作和可观察的完成依据。先区分互斥情境，再为每个情境设计能独立完成的终点。requires 中的每一项及其祖先都必须完成，是 AND，不是任选一项。以草稿、审核中和新补正为例，三种状态分别以知道如何提交、知道在哪里等反馈、知道如何核对新提示作为终点。把确认下一步写入各自的 done_when，不要添加同时依赖三个方案的统一确认节点。default_leaf 必须是上述某个终点，其 id 不出现在任何节点的 requires 中。
用户可以从业务中途进入。一个共享的状态核对节点应该接受各分支对应的现状，不能只接受其中一种。对方已明确自述时可直接通过核对。等待反馈的路径只依赖核对现状，不经过旧退回原因、修改、上传或提交教程。多笔独立办理时，区分各笔状态；若原文说明全部办完后的处理方式，给它单独的叶子，只依赖核对各笔状态或一个统合的状态核对，不依赖逐笔解释如何办理或逐笔说明等反馈。
每个叶子 goal 描述对方最后弄明白的事。仅告知下一步的任务，用户说清自己理解的做法即可完成沟通。background 和节点动作都以原工单为依据，保留条件和例外。不添加工单未提供的验证材料、按钮位置或页面变化。"""


class Prerequisites(BaseModel):
    id: NodeId
    requires: list[NodeId] = Field(max_length=16)


class DependencyReview(BaseModel):
    changes: list[Prerequisites] = Field(default_factory=list, max_length=28)
    remove_merges: list[NodeId] = Field(default_factory=list, max_length=8)
    default_leaf: NodeId | None = None

    def revise(self, graph: Graph) -> Graph:
        changes = {entry.id: entry.requires for entry in self.changes}
        removed = set(self.remove_merges)
        leaves = set(graph.leaves())
        merge_leaves = {n.id for n in graph.nodes if n.kind == "business" and n.id in leaves
                        and len(n.requires) >= 2}
        if (len(changes) != len(self.changes) or not removed <= merge_leaves
                or removed.intersection(changes) or (self.default_leaf and graph.default_leaf not in removed)):
            raise ValueError("无效的流程调整")
        deps = graph.dependencies()
        ancestors = {}
        for key in TopologicalSorter(deps).static_order():
            ancestors[key] = set(deps[key]).union(*(ancestors[p] for p in deps[key]))
        editable = {n.id for n in graph.nodes if n.kind == "business"}
        if not all(key in editable and set(parents) <= ancestors[key] for key, parents in changes.items()):
            raise ValueError("前置节点必须来自当前路径")
        return Graph(default_leaf=self.default_leaf or graph.default_leaf, nodes=[
            n.model_copy(update={"requires": changes[n.id]}) if n.id in changes else n
            for n in graph.nodes if n.id not in removed
        ])


REVIEW_PROMPT = """检查每个叶子的全部祖先。requires 是 AND。分别模拟处于各单一情境的来电者，检查是否误要求他们先完成其他情境的操作。若一个叶子只是把草稿、已提交等互斥结果统一确认，且各分支最后的节点本身已有足够的完成依据，在 remove_merges 列出这个多前置的叶子，让各分支独立收尾。保留全部工单要点和单独的多笔办完结果；删除默认叶子时在 default_leaf 指定仍然存在的合适叶子。
再检查中途进入。能从对方自述获得的状态不依赖讲解旧问题。等待一笔或多笔已提交的结果，只需核对相应状态，无需讲原来的附件为什么有错、怎样重新提交或逐笔确认是否听懂。用 changes 替换这些节点的直接前置，替换项只允许是该节点原有的祖先，没有必要前置时用空数组。真实的共同前置和主路径保留。只输出 JSON：{"changes":[{"id":"节点","requires":["修改后的直接前置"]}],"remove_merges":[],"default_leaf":null}。无需调整时两个数组为空。"""


class Credit(BaseModel):
    id: NodeId
    quote: ShortText


class Route(BaseModel):
    leaf: NodeId
    completed: list[Credit] = Field(default_factory=list, max_length=16)
    reopen: list[Credit] = Field(default_factory=list, max_length=16)
    need: str = Field(default="", max_length=200)
    next_node: NodeId | None = None

    @field_validator("completed", "reopen", mode="before")
    @classmethod
    def discard_unquoted_ids(cls, value):
        # A bare ID can never change progress. Keep the usable leaf and evidence.
        return [credit for credit in value if not isinstance(credit, str)] if isinstance(value, list) else value


ROUTE_FIELDS = '{"leaf":"有效的叶子id","completed":[],"reopen":[],"need":"本轮诉求","next_node":null}'
ROUTE_RULES = """从 leaves 选择能解决最新诉求的终点。同一句既报告某件事已办完，又追问另一件事时，按追问指向的事项选终点。改口以最新明确的现状为准，并撤销被改口推翻的已完成节点，即使它属于旧路径。计划去做、询问能否做和已经做完是不同的状态。复述已解释的下一步时，沿此前适用的路径确认理解。若用户已给出具体状态或材料，直接检查当前路径上哪些核对节点的 done_when 已满足，避免再问已知情况。
completed 和 reopen 都是对象数组，每项为 {"id":"业务节点id","quote":"用户连续原话"}，禁止只填节点 ID，没有证据就留空数组。completed 只填所选路径的新完成项，原话结合此前已完整播出的说明必须满足 done_when。某个前置是核对事实时，可引用此前用户明确报告且没有改口的最新状态。例如前一轮已确认一单提交，本轮确认另一单也提交，可分别引用两轮的原话完成对应状态核对。每条 quote 只摘一轮的连续原话，不能拼接。reopen 和完成终点时，仅摘最新一句中的改口或确认部分，前几轮只作上下文。用户还在询问同一事项时，不用本轮尚未播出的回复完成终点。接通和问候由系统记录。只说嗯、好、是的不足以完成业务目标；简短肯定可回答刚听到的明确是非问句。next_node 填所选路径上最贴合诉求且前置已满足的节点，无法确定或已经完成时填 null。"""
ROUTE_PROMPT = "你为正在进行的电话选择任务图的终点并评估节点。只输出 JSON：\n" + ROUTE_FIELDS + "\n" + ROUTE_RULES


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
    opening_status: Literal["pending", "playing", "delivered", "interrupted"] = "pending"
    playbook: CallPlaybook | None = None
    revoked: set[str] = field(default_factory=set)

    def __post_init__(self):
        self.leaf = self.leaf or self.file.graph.default_leaf

    def accept(self, user: str, previous_question: str = ""):
        if self.opening_status != "delivered":
            self.opening_status = "interrupted"
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

    def apply(self, route: Route, user: str, *, earlier_users: tuple[str, ...] = ()):
        graph = self.file.graph
        if route.leaf not in graph.leaves():
            return
        previous_node = self.context()["current"]
        self.judged = True
        self.leaf = route.leaf
        self.preferred = route.next_node
        self.need = route.need or self.need
        deps = graph.dependencies()
        path = self.path()
        root, greeting = (n.id for n in graph.nodes[:2])
        entry = {root, greeting}
        revoked = {v.id for v in route.reopen if v.id in deps and v.id not in entry
                   and v.quote.strip() and v.quote in user}
        self.revoked.update(revoked)
        passed = set(self.done) | (entry if self.opening_status in {"delivered", "interrupted"} else set())
        asking = re.search(r"[？?]|(?:^|[。！；，,])\s*(?:请问|为什么|怎么|如何|还要不要|还用不用)"
                           r"|(?:吗|么|怎么办|行不行|对不对)[呀啊呢。！!\s]*$", user)
        approvals = {}
        for credit in route.completed:
            if credit.id not in path or not credit.quote.strip():
                continue
            from_current = credit.quote in user
            if not from_current and (credit.id == self.leaf or credit.id in self.done or credit.id in self.revoked
                                     or not any(credit.quote in text for text in earlier_users)):
                continue
            # The caller can establish the current status while still asking
            # what to do with it. Keep the terminal goal open in that case.
            if credit.id == self.leaf and asking:
                continue
            if re.fullmatch(r"[嗯啊哦好的是对行可以，,。！!\s]+", credit.quote):
                # A yes can answer the identity/availability question just heard.
                # It cannot acknowledge other nodes or an unspoken question.
                if (not from_current or credit.id != previous_node
                        or not re.search(r"(?:有没有|是不是|是否)|(?:吗|么|对吧|是吧|没有)[？?\s]*$", self.previous_question)
                        or not re.search(r"是|对|可以|方便", credit.quote)):
                    continue
            approvals[credit.id] = credit.quote
        for key in TopologicalSorter(deps).static_order():
            if key in entry:
                continue
            if key in revoked or not all(p in passed for p in deps[key]):
                passed.discard(key)
                self.done.pop(key, None)
            elif key in approvals and self.event not in {"没听懂", "需要全貌"}:
                self.done[key] = approvals[key]
                self.revoked.discard(key)
                passed.add(key)

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
        root, greeting = (n.id for n in graph.nodes[:2])
        completed = ([root] if self.opening_status != "pending" else [])
        if self.opening_status == "delivered":
            completed.append(greeting)
        completed.extend(self.done)
        skipped = [greeting] if self.opening_status == "interrupted" else []
        passed = set(completed + skipped)
        ready = [key for key in path if key not in passed and all(p in passed for p in nodes[key].requires)]
        current = self.preferred if self.preferred in ready else next(iter(ready), None)
        node = nodes[current or self.leaf]
        return {"file": self.file.fingerprint(), "leaf": self.leaf, "final_goal": nodes[self.leaf].goal,
                "current": current, "goal": node.goal, "action": node.action or node.goal, "done_when": node.done_when, "available": ready,
                "checkpoints": node.checkpoints, "completed": completed, "skipped": skipped, "path": path,
                "need": self.need, "event": self.event,
                **({"repair": "先用一两句交代为什么打来、这件事与对方有什么关系以及对方要做什么。对方此刻在问来意，避免重复上一轮的文件清单和公司名。"} if self.event == "需要全貌" else
                   {"repair": "用白话换一个例子解释卡住的概念。对方听懂前保留这个节点。"} if self.event == "没听懂" else {}), "finished": self.leaf in self.done,
                **({"heard_together": self.heard_together} if self.heard_together else {}),
                **({"playbook": self.playbook.model_dump(exclude={"opening"})} if self.playbook else {})}

    def candidates(self, options):
        if self.event:
            return []
        scope = self.context()
        points = scope["checkpoints"]
        return [c for c in options if not c.checkpoint or not points or c.checkpoint in points]


def model_context(snapshot: dict) -> dict:
    # The card is used by deterministic matchers only. No model receives it.
    data = dict(snapshot)
    card = data.pop("task", None)
    briefing = data.pop("briefing", None)
    if briefing is None and card:
        briefing = compile_phone(TaskCard.model_validate(card)).briefing()
    return {**(briefing or {}), **data}


async def model_json(provider, prompt, data, schema, tokens, *, cache=True, allow_large_context=False):
    content = json.dumps(data, ensure_ascii=False)
    messages = [{"role": "system", "content": prompt}, {"role": "user", "content": content}]
    kwargs = {"schema": schema.model_json_schema()} if getattr(provider, "is_local", False) else {}
    if kwargs and allow_large_context and len(prompt) + len(content) + tokens > 8192:
        kwargs["context_size"] = 16384
    if kwargs and schema is CompiledMaterials:
        # Ollama's grammar rejects the long repetition for this field. The
        # Pydantic model still enforces its length on the returned text.
        kwargs["schema"]["properties"]["background"].pop("maxLength")
    if not cache and not getattr(provider, "is_local", False):
        kwargs["use_cache"] = False
    text = "".join([part async for part in provider.chat(messages, max_tokens=tokens, **kwargs)]).strip()
    text = re.sub(r"^\x60{3}(?:json)?\s*|\s*\x60{3}$", "", text)
    return schema.model_validate_json(text)
