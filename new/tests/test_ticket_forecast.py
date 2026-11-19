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
        "title": "报修咨询",
        "goal": "说明怎么报修",
        "facts": "需要检修时联系运营商官方客服报修。",
        "checkpoints": [{"id": "next", "description": "说明怎么报修"}],
        "opening": "您好，需要我说明怎么报修吗？",
    })


# --- generator: families, labels, coverage ---------------------------------

def test_jd_card_generates_every_required_family_with_labels():
    items = generate_ticket_candidates(card("jd-qualification.json"))
    by_intent = {item.intent: item for item in items}
    assert {
        "missing_material", "how_to_submit", "authorization", "name_mismatch",
        "screenshot", "saved", "submitted", "in_review", "timing",
    } <= set(by_intent)
    assert by_intent["authorization"].checkpoint == "authorization"
    assert by_intent["name_mismatch"].checkpoint == "company_name"
    assert by_intent["screenshot"].checkpoint == "trademark"
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


def test_question_families_cover_all_four_scenarios_without_borrowing_facts():
    expectations = {
        "jd": (
            card("jd-qualification.json"),
            {"missing_material", "how_to_submit", "authorization",
             "name_mismatch", "screenshot", "saved", "submitted",
             "in_review", "timing"},
            set(),
        ),
        "official": (
            official_card(),
            {"missing_material", "how_to_submit", "in_review", "timing"},
            {"authorization", "name_mismatch", "screenshot", "saved", "submitted"},
        ),
        "broadband": (
            card("home-broadband-support.json"),
            {"how_to_submit", "timing"},
            {"authorization", "name_mismatch", "screenshot", "saved",
             "submitted", "in_review"},
        ),
        "network": (
            card("network-consultation.json"),
            {"how_to_submit", "timing"},
            {"authorization", "name_mismatch", "screenshot", "saved",
             "submitted", "in_review"},
        ),
    }
    for name, (task, required, forbidden) in expectations.items():
        items = generate_ticket_candidates(task)
        assert required <= intents(items), (name, intents(items))
        assert not (forbidden & intents(items)), (name, intents(items))
        assert 1 <= len(items) <= DEFAULT_MAX
        assert len({item.id for item in items}) == len(items)
        assert len({item.reply.text for item in items}) == len(items)
        examples = [example for item in items for example in item.examples]
        assert len(examples) == len(set(examples)), name


def test_generated_examples_are_selectable_by_existing_matcher():
    scenarios = [
        (card("jd-qualification.json"), "缺什么材料？", "missing_material"),
        (card("jd-qualification.json"), "已经提交了。", "submitted"),
        (official_card(), "多久能出结果？", "timing"),
        (card("home-broadband-support.json"), "怎么报修？", "how_to_submit"),
        (card("network-consultation.json"), "怎么安装？", "how_to_submit"),
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
                 card("network-consultation.json"), DEFAULT_CARD):
        for item in generate_ticket_candidates(task):
            assert prediction_text_allowed(item.reply.text), item.reply.text


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
    tickets = generate_ticket_candidates(card("jd-qualification.json"))
    model = [candidate(f"p{i}", f"模型候选{i}。") for i in range(6)]
    merged = merge_candidates(tickets, model, DEFAULT_CANDIDATE_LIMIT)
    assert len(merged) == 10
    assert [item.id for item in merged[:len(tickets)]] == [item.id for item in tickets]
    assert [item.id for item in merged[len(tickets):]] == ["p0"]


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


async def test_nine_ticket_candidates_keep_the_model_reserve():
    task = card("jd-qualification.json")
    tickets = generate_ticket_candidates(task)
    assert len(tickets) == 9
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
