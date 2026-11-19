from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.brain import Brain
from app.context_aware_decision import ContextAwareDecisionEngine
from app.forecast import (
    DEFAULT_CANDIDATE_LIMIT,
    OpeningForecastCache,
    candidate_limit_from_env,
    merge_candidates,
    model_reserve,
    ticket_quota,
)
from app.models import DEFAULT_CARD, Candidate, Memory, Say, TaskCard
from app.ticket_forecast import (
    DEFAULT_MAX,
    enabled,
    excluded_intents_from_env,
    generate_ticket_candidates,
    max_candidates_from_env,
)

ROOT = Path(__file__).resolve().parents[1]


def card(name: str) -> TaskCard:
    return TaskCard.model_validate_json((ROOT / "tasks" / name).read_text(encoding="utf-8"))


def official_card() -> TaskCard:
    """The official work-order scenario is not a shipped task card in this branch."""
    return TaskCard.model_validate({
        "title": "官方工单进度咨询",
        "goal": "说明工单当前处理进度、下一步和预计时间，不承诺一定通过或加急成功。",
        "facts": "本任务讨论已受理的官方工单。工单已受理，当前在审核中；页面显示预计一个工作日，但具体以实际审核为准。可以补充材料，但不能保证今天通过或加急成功。",
        "checkpoints": [
            {"id": "progress", "description": "说明工单当前处于审核中"},
            {"id": "next", "description": "说明按页面提示补充材料或等待"},
            {"id": "limit", "description": "说明不能承诺具体出结果时间"},
        ],
        "opening": "您好，这里是工单进度咨询。您想了解当前进度，还是补充材料？",
    })


def cargo_card() -> TaskCard:
    """Mirror of integration/prediction-rules new/tasks/logistics-delivery.json."""
    return TaskCard.model_validate({
        "title": "快递物流异常",
        "goal": "说明快递未收到、物流停滞、改地址或签收异常的下一步，不承诺具体派送时间。",
        "facts": "【场景资料】本任务讨论快递物流异常。当前可以查询到运单状态和最近一条物流记录；"
                 "看不到更细的网点内部进度。可以建议联系承运快递员或官方客服，"
                 "但不能承诺今天送达、不能直接修改地址。",
        "checkpoints": [
            {"id": "status", "description": "说明当前运单状态和最近物流记录"},
            {"id": "contact", "description": "说明联系快递员或官方客服的方式边界"},
            {"id": "address", "description": "说明改地址需要联系承运方，不能直接保证成功"},
        ],
        "opening": "您好，这里是物流异常咨询。您是没收到件，还是物流一直没更新？",
    })


def invoice_card() -> TaskCard:
    """Mirror of integration/prediction-rules new/tasks/after-sales-invoice.json."""
    return TaskCard.model_validate({
        "title": "发票开具咨询",
        "goal": "说明发票类型、抬头、开具状态和重开方式，不承诺具体开票时间。",
        "facts": "【场景资料】本任务讨论发票咨询。当前只能看到申请记录和页面状态，"
                 "不能直接开票或改抬头。普通发票和专用发票需要的资料不同；"
                 "抬头填错通常需要先作废原申请再重新提交，具体以页面提示为准。",
        "checkpoints": [
            {"id": "type", "description": "说明普通发票和专用发票的区别"},
            {"id": "header", "description": "说明发票抬头需要与主体信息一致"},
            {"id": "status", "description": "说明开票状态和重开方式"},
        ],
        "opening": "您好，这里是发票咨询。您是想开发票，还是发票抬头/类型有问题？",
    })


def candidate(cid: str, text: str, *, when: str = "用户询问",
              checkpoint: str | None = None, intent: str | None = None,
              polarity: str | None = None) -> Candidate:
    return Candidate(id=cid, when=when, reply=Say(text=text),
                     checkpoint=checkpoint, intent=intent, polarity=polarity)


def intents(items: list[Candidate]) -> set[str]:
    return {item.intent for item in items}


def single_ticket_card() -> TaskCard:
    """Only the how_to_submit family applies, so the generator emits one item."""
    return TaskCard.model_validate({
        "title": "办理咨询",
        "goal": "说明下一步怎么做",
        "facts": "请按页面提示继续办理。",
        "checkpoints": [{"id": "next", "description": "说明下一步怎么做"}],
        "opening": "您好，需要我说明下一步吗？",
    })


# --- generator: families, labels, coverage ---------------------------------

def test_jd_card_generates_every_required_family_with_labels():
    items = generate_ticket_candidates(card("jd-qualification.json"))
    by_intent = {item.intent: item for item in items}
    assert {
        "missing_material", "how_to_submit", "upload_issue", "authorization",
        "name_mismatch", "screenshot", "saved", "submitted", "in_review",
        "timing",
    } <= set(by_intent)
    assert by_intent["authorization"].checkpoint == "authorization"
    assert by_intent["name_mismatch"].checkpoint == "company_name"
    assert by_intent["screenshot"].checkpoint == "trademark"
    assert by_intent["upload_issue"].checkpoint == "submission"
    assert by_intent["saved"].polarity == "statement"
    assert by_intent["submitted"].polarity == "statement"
    assert by_intent["timing"].polarity == "question"
    for item in items:
        assert item.id.startswith("t_")
        assert item.intent and item.polarity
        assert 1 <= len(item.examples) <= 2
        # A deterministic template only describes the reply; delivery credit
        # still comes from the model's state update.
        assert item.reply.covers == []
        assert item.reply.question == ""


ALL_INTENTS = {
    "missing_material", "how_to_submit", "upload_issue", "authorization",
    "name_mismatch", "screenshot", "saved", "submitted", "in_review",
    "handoff", "connectivity", "slow", "router", "device_check",
    "optical_light", "repair", "price", "recommend", "install", "contract",
    "coverage", "promotion", "logistics_status", "logistics_contact",
    "logistics_address", "lost_or_damaged", "invoice_type", "invoice_header",
    "invoice_status", "timing",
}


def scenario_families() -> dict[str, tuple[TaskCard, set[str]]]:
    return {
        "jd": (
            card("jd-qualification.json"),
            {"missing_material", "how_to_submit", "upload_issue",
             "authorization", "name_mismatch", "screenshot", "saved",
             "submitted", "in_review", "timing"},
        ),
        "official": (
            official_card(),
            {"missing_material", "how_to_submit", "in_review", "handoff",
             "timing"},
        ),
        "broadband": (
            card("home-broadband-support.json"),
            {"how_to_submit", "connectivity", "slow", "router",
             "device_check", "optical_light", "repair", "timing"},
        ),
        "network": (
            card("network-consultation.json"),
            {"how_to_submit", "price", "recommend", "install", "contract",
             "coverage", "promotion", "timing"},
        ),
        "logistics": (
            cargo_card(),
            {"logistics_status", "logistics_contact", "logistics_address",
             "lost_or_damaged", "timing"},
        ),
        "invoice": (
            invoice_card(),
            {"invoice_type", "invoice_header", "invoice_status", "timing"},
        ),
    }


def test_question_families_cover_all_scenarios_without_borrowing_facts():
    families = scenario_families()
    assert set().union(*(expected for _, expected in families.values())) <= ALL_INTENTS
    for name, (task, expected) in families.items():
        items = generate_ticket_candidates(task)
        # Exact set equality is the cross-scenario guard: a family borrowed
        # from another card would show up here.
        assert intents(items) == expected, (name, intents(items))
        assert 1 <= len(items) <= DEFAULT_MAX
        assert len({item.id for item in items}) == len(items)
        assert len({item.reply.text for item in items}) == len(items)
        examples = [example for item in items for example in item.examples]
        assert len(examples) == len(set(examples)), name
        for item in items:
            assert item.checkpoint is None or item.checkpoint in {
                point.id for point in task.checkpoints}
            assert item.intent and item.polarity


# Integration audit recommendations, and the baseline pool each card must keep
# even after those intents are excluded.
RECOMMENDED_EXCLUSIONS = {
    "jd": (["how_to_submit"], 9),
    "official": ([], 5),
    "broadband": (["how_to_submit", "timing"], 5),
    "network": (["how_to_submit", "timing"], 5),
    "logistics": (["timing"], 4),
    "invoice": ([], 4),
}
REPLACEMENT_INTENTS = {
    "jd": {"upload_issue"},
    "broadband": {"device_check", "optical_light", "repair"},
    "network": {"contract", "coverage", "promotion"},
    "logistics": {"lost_or_damaged"},
}


def test_excluding_recommended_intents_does_not_shrink_the_pool():
    families = scenario_families()
    for name, (excluded, baseline) in RECOMMENDED_EXCLUSIONS.items():
        task = families[name][0]
        default = generate_ticket_candidates(task)
        filtered = generate_ticket_candidates(task, exclude=excluded)
        assert len(default) >= baseline, (name, len(default))
        assert len(filtered) >= baseline, (name, len(filtered))
        assert not (set(excluded) & intents(filtered)), name
        assert REPLACEMENT_INTENTS.get(name, set()) <= intents(filtered), name
        if not excluded:
            assert filtered == default, name


def test_replacement_families_are_selectable_and_grounded():
    scenarios = [
        (card("jd-qualification.json"), "上传失败了怎么办？", "upload_issue"),
        (card("home-broadband-support.json"),
         "怎么判断是一台还是多台设备？", "device_check"),
        (card("home-broadband-support.json"), "光猫亮红灯怎么办？", "optical_light"),
        (card("home-broadband-support.json"), "报修找谁？", "repair"),
        (card("network-consultation.json"), "有合约吗？", "contract"),
        (card("network-consultation.json"), "能覆盖全屋吗？", "coverage"),
        (card("network-consultation.json"), "有优惠吗？", "promotion"),
        (cargo_card(), "快递一直没收到。", "lost_or_damaged"),
    ]
    engine = ContextAwareDecisionEngine()
    for task, utterance, expected in scenarios:
        items = generate_ticket_candidates(task)
        decision, selected = engine.should_reuse(utterance, items, "", task)
        assert decision == "reuse", (utterance, decision)
        assert selected is not None and selected.intent == expected, utterance


def test_new_scenario_families_are_selectable_and_grounded():
    scenarios = [
        (card("jd-qualification.json"), "缺什么材料？", "missing_material"),
        (card("jd-qualification.json"), "已经提交了。", "submitted"),
        (official_card(), "多久能出结果？", "timing"),
        (card("home-broadband-support.json"), "怎么报修？", "how_to_submit"),
        (card("network-consultation.json"), "怎么安装？", "how_to_submit"),
        (cargo_card(), "到哪了？", "logistics_status"),
        (cargo_card(), "物流一直没更新。", "logistics_status"),
        (cargo_card(), "快递员电话多少？", "logistics_contact"),
        (cargo_card(), "能改地址吗？", "logistics_address"),
        (invoice_card(), "普票和专票有什么区别？", "invoice_type"),
        (invoice_card(), "抬头写错了怎么办？", "invoice_header"),
        (invoice_card(), "开票到哪一步了？", "invoice_status"),
        (card("home-broadband-support.json"), "上不了网怎么办？", "connectivity"),
        (card("home-broadband-support.json"), "网速很慢怎么办？", "slow"),
        (card("home-broadband-support.json"), "路由器怎么重启？", "router"),
        (card("network-consultation.json"), "300M 多少钱？", "price"),
        (card("network-consultation.json"), "推荐哪个套餐？", "recommend"),
        (card("network-consultation.json"), "什么时候能装？", "install"),
        (official_card(), "材料交到哪？", "handoff"),
        (official_card(), "能催一下吗？", "handoff"),
    ]
    engine = ContextAwareDecisionEngine()
    for task, utterance, expected in scenarios:
        items = generate_ticket_candidates(task)
        decision, selected = engine.should_reuse(utterance, items, "", task)
        assert decision == "reuse", (utterance, decision)
        assert selected is not None and selected.intent == expected, utterance


def test_generated_replies_pass_the_prediction_compliance_filter():
    from app.session import prediction_text_allowed

    for task in (card("jd-qualification.json"), official_card(),
                 card("home-broadband-support.json"),
                 card("network-consultation.json"), cargo_card(),
                 invoice_card(), DEFAULT_CARD):
        for item in generate_ticket_candidates(task):
            assert prediction_text_allowed(item.reply.text), item.reply.text


def test_exclude_switch_skips_only_named_intents(monkeypatch):
    task = card("home-broadband-support.json")
    monkeypatch.delenv("TICKET_FORECAST_EXCLUDE", raising=False)
    baseline = generate_ticket_candidates(task)
    monkeypatch.setenv("TICKET_FORECAST_EXCLUDE", "connectivity, slow")
    filtered = generate_ticket_candidates(task)
    assert [item.intent for item in filtered] == [
        item.intent for item in baseline
        if item.intent not in {"connectivity", "slow"}]
    assert {"connectivity", "slow"} & intents(filtered) == set()

    # An explicit argument wins over the environment.
    assert generate_ticket_candidates(task, exclude=[]) == baseline
    # Unknown names and empty values are harmless.
    monkeypatch.setenv("TICKET_FORECAST_EXCLUDE", " not_a_family ,, ")
    assert generate_ticket_candidates(task) == baseline
    assert excluded_intents_from_env() == {"not_a_family"}


def test_generation_is_deterministic_and_never_calls_a_model():
    task = card("jd-qualification.json")
    first = generate_ticket_candidates(task)
    second = generate_ticket_candidates(task)
    assert first == second
    assert not hasattr(generate_ticket_candidates, "chat")


# --- labels: backward compatibility ----------------------------------------

def test_candidate_labels_are_optional_and_old_records_still_validate():
    old = {
        "id": "p0_0", "when": "用户确认继续",
        "examples": ["好的。"],
        "reply": {"text": "那我继续说。", "covers": ["reason"], "question": ""},
    }
    parsed = Candidate.model_validate(old)
    assert parsed.checkpoint is None
    assert parsed.intent is None
    assert parsed.polarity is None
    assert Candidate.model_validate(parsed.model_dump()) == parsed


def test_forecast_cache_round_trips_labeled_ticket_candidates():
    cache = OpeningForecastCache()
    item = generate_ticket_candidates(card("jd-qualification.json"))[0]
    cache.store("task", [item], complete=True)
    restored, complete = cache.get("task")
    assert complete is True
    assert restored == [item]
    assert restored[0].checkpoint == item.checkpoint
    assert restored[0].intent == item.intent
    assert restored[0].polarity == item.polarity


# --- merge: dedupe and cap --------------------------------------------------

def test_merge_deduplicates_id_text_and_labels():
    base = [candidate("t_authorization", "缺的是上游授权。",
                      checkpoint="authorization", intent="authorization",
                      polarity="question")]
    incoming = [
        candidate("t_authorization", "完全不同的文本。"),             # duplicate id
        candidate("model_1", "缺的是上游授权。"),                     # same text
        candidate("t_authorization_2", "另一句回复。",
                  checkpoint="authorization", intent="authorization",
                  polarity="question"),                              # same labels
        candidate("model_2", "模型新回复。"),                         # fresh
    ]
    merged = merge_candidates(base, incoming, DEFAULT_CANDIDATE_LIMIT)
    assert [item.id for item in merged] == ["t_authorization", "model_2"]


def test_merge_enforces_the_ten_candidate_cap():
    tickets = generate_ticket_candidates(card("network-consultation.json"))
    assert len(tickets) == 8
    model = [candidate(f"p{i}", f"模型候选{i}。") for i in range(6)]
    merged = merge_candidates(tickets, model, DEFAULT_CANDIDATE_LIMIT)
    assert len(merged) == 10
    assert [item.id for item in merged[:len(tickets)]] == [item.id for item in tickets]
    assert [item.id for item in merged[len(tickets):]] == ["p0", "p1"]


def test_feature_switch_and_limits_follow_the_environment(monkeypatch):
    monkeypatch.delenv("TICKET_FORECAST_ENABLED", raising=False)
    assert enabled() is True
    for value in ("0", "false", "OFF", "no"):
        monkeypatch.setenv("TICKET_FORECAST_ENABLED", value)
        assert enabled() is False
    monkeypatch.setenv("TICKET_FORECAST_ENABLED", "1")
    assert enabled() is True

    monkeypatch.setenv("TICKET_FORECAST_MAX", "4")
    assert max_candidates_from_env() == 4
    assert len(generate_ticket_candidates(card("jd-qualification.json"))) == 4
    monkeypatch.setenv("TICKET_FORECAST_MAX", "not-a-number")
    assert max_candidates_from_env() == DEFAULT_MAX

    monkeypatch.setenv("PREDICTION_CANDIDATE_LIMIT", "3")
    assert candidate_limit_from_env() == 3
    monkeypatch.setenv("PREDICTION_CANDIDATE_LIMIT", "0")
    assert candidate_limit_from_env() == DEFAULT_CANDIDATE_LIMIT


# --- Brain integration ------------------------------------------------------

class OneCandidateCloud:
    def __init__(self):
        self.calls = 0

    async def chat(self, messages, **kwargs):
        self.calls += 1
        yield json.dumps({"candidates": [{
            "id": "c1", "when": "用户确认继续",
            "reply": {"text": f"好的，那我继续说第{self.calls}件事。", "covers": []},
        }]}, ensure_ascii=False)


class FailingCloud:
    async def chat(self, messages, **kwargs):
        raise RuntimeError("model unavailable")
        yield ""  # pragma: no cover - makes this an async generator


async def test_brain_publishes_ticket_batch_then_streams_model_candidates():
    cloud = OneCandidateCloud()
    brain = Brain(cloud, forecast_branches=2)
    batches = [batch async for batch in brain.predict_batches(Brain.snapshot(Memory(DEFAULT_CARD)))]
    tickets = generate_ticket_candidates(DEFAULT_CARD)
    assert batches[0] == tickets
    assert all(len(batch) == 1 for batch in batches[1:])
    streamed = [item for batch in batches[1:] for item in batch]
    assert [item.id for item in streamed] == ["p0_0", "p1_0"]
    ids = [item.id for batch in batches for item in batch]
    assert len(ids) == len(set(ids))
    assert cloud.calls == 2


async def test_switch_off_keeps_the_model_only_stream():
    cloud = OneCandidateCloud()
    brain = Brain(cloud, forecast_branches=1, ticket_forecasts=False)
    batches = [batch async for batch in brain.predict_batches(Brain.snapshot(Memory(DEFAULT_CARD)))]
    items = [item for batch in batches for item in batch]
    assert [item.id for item in items] == ["p0_0"]


def test_model_reserve_and_ticket_quota_stay_inside_the_pool():
    for limit, reserve, quota in (
        (1, 1, 0), (2, 1, 1), (3, 1, 2), (6, 2, 4), (10, 3, 7), (30, 4, 26),
    ):
        assert model_reserve(limit) == reserve, limit
        assert ticket_quota(limit) == quota, limit
        assert ticket_quota(limit) + model_reserve(limit) <= max(1, limit)


async def test_ten_ticket_candidates_keep_the_model_reserve():
    task = card("jd-qualification.json")
    tickets = generate_ticket_candidates(task)
    assert len(tickets) == 10
    brain = Brain(OneCandidateCloud(), forecast_branches=5)
    batches = [batch async for batch in brain.predict_batches(Brain.snapshot(Memory(task)))]
    items = [item for batch in batches for item in batch]

    admitted = ticket_quota(DEFAULT_CANDIDATE_LIMIT)
    reserve = model_reserve(DEFAULT_CANDIDATE_LIMIT)
    assert admitted == 7 and reserve == 3
    # Ticket text is still published first, then model candidates stream in.
    assert batches[0] == [item for item in tickets[:admitted]]
    assert all(len(batch) == 1 for batch in batches[1:])
    assert [item.id for item in items[:admitted]] == [
        item.id for item in tickets[:admitted]]
    model_items = items[admitted:]
    assert len(model_items) >= reserve
    assert all(item.id.startswith("p") for item in model_items)
    assert len(items) <= DEFAULT_CANDIDATE_LIMIT


async def test_single_ticket_leaves_the_remaining_slots_to_the_model():
    task = single_ticket_card()
    tickets = generate_ticket_candidates(task)
    assert len(tickets) == 1
    items = await Brain(OneCandidateCloud(), forecast_branches=5).predict(
        Brain.snapshot(Memory(task)))
    assert [item.id for item in items[:1]] == [tickets[0].id]
    model_items = items[1:]
    assert len(model_items) == 5
    assert len(items) <= DEFAULT_CANDIDATE_LIMIT
    assert all(item.id.startswith("p") for item in model_items)


async def test_five_ticket_candidates_leave_five_model_slots():
    task = official_card()
    tickets = generate_ticket_candidates(task)
    assert len(tickets) == 5
    items = await Brain(OneCandidateCloud(), forecast_branches=5).predict(
        Brain.snapshot(Memory(task)))
    assert len(items) == 10
    assert sum(item.id.startswith("t_") for item in items) == 5
    assert sum(item.id.startswith("p") for item in items) == 5
    assert [item.id for item in items[:5]] == [item.id for item in tickets]


async def test_eight_ticket_candidates_still_keep_the_model_reserve():
    task = card("home-broadband-support.json")
    tickets = generate_ticket_candidates(task)
    assert len(tickets) == 8
    items = await Brain(OneCandidateCloud(), forecast_branches=5).predict(
        Brain.snapshot(Memory(task)))
    assert len(items) == 10
    admitted = ticket_quota(DEFAULT_CANDIDATE_LIMIT)
    assert sum(item.id.startswith("t_") for item in items) == admitted
    assert sum(item.id.startswith("p") for item in items) == 10 - admitted
    assert 10 - admitted >= model_reserve(DEFAULT_CANDIDATE_LIMIT)


async def test_merged_cap_is_ten_even_when_tickets_fill_most_slots():
    cloud = OneCandidateCloud()
    brain = Brain(cloud, forecast_branches=5)
    items = await brain.predict(Brain.snapshot(Memory(DEFAULT_CARD)))
    tickets = generate_ticket_candidates(DEFAULT_CARD)
    assert len(items) == 10
    assert [item.id for item in items[:len(tickets)]] == [item.id for item in tickets]
    assert [item.id for item in items[len(tickets):]] == [
        "p0_0", "p1_0", "p2_0", "p3_0"]


async def test_model_failure_keeps_already_published_ticket_batch():
    brain = Brain(FailingCloud(), forecast_branches=1)
    published: list[Candidate] = []
    with pytest.raises(RuntimeError):
        async for batch in brain.predict_batches(Brain.snapshot(Memory(DEFAULT_CARD))):
            published.extend(batch)
    # Session publishes each batch as it arrives, so the caller keeps usable
    # ticket text even though the model branch failed afterwards.
    assert published == generate_ticket_candidates(DEFAULT_CARD)


async def test_opening_cache_reuses_model_candidates_and_regenerates_tickets():
    cloud = OneCandidateCloud()
    cache = OpeningForecastCache()
    snapshot = {**Brain.snapshot(Memory(DEFAULT_CARD)), "opening": True}
    first = await Brain(cloud, cache).predict(snapshot)
    calls = cloud.calls
    second = await Brain(cloud, cache).predict(snapshot)
    assert cloud.calls == calls
    assert second == first
    assert any(item.id.startswith("t_") for item in second)


async def test_switching_the_feature_off_does_not_hit_the_other_arm_cache():
    cloud = OneCandidateCloud()
    cache = OpeningForecastCache()
    snapshot = {**Brain.snapshot(Memory(DEFAULT_CARD)), "opening": True}
    with_tickets = await Brain(cloud, cache, ticket_forecasts=True).predict(snapshot)
    calls = cloud.calls
    without_tickets = await Brain(cloud, cache, ticket_forecasts=False).predict(snapshot)
    assert cloud.calls > calls
    assert any(item.id.startswith("t_") for item in with_tickets)
    assert not any(item.id.startswith("t_") for item in without_tickets)


async def test_exclude_switch_is_part_of_the_opening_cache_arm(monkeypatch):
    cloud = OneCandidateCloud()
    cache = OpeningForecastCache()
    task = card("home-broadband-support.json")
    snapshot = {**Brain.snapshot(Memory(task)), "opening": True}
    monkeypatch.setenv("TICKET_FORECAST_EXCLUDE", "router")
    first = await Brain(cloud, cache).predict(snapshot)
    calls = cloud.calls
    monkeypatch.setenv("TICKET_FORECAST_EXCLUDE", "")
    second = await Brain(cloud, cache).predict(snapshot)
    assert cloud.calls > calls
    assert "router" not in intents(first)
    assert "router" in intents(second)
