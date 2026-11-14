"""Tests for the predictive pre-generation/match/filler pipeline."""
from app.predictive import create_pipeline
from app.predictive.rules import InMemoryCandidateStore, PresetCandidateFactory, RuleClassifier

CTX = {"reject_l3": "主体资质到期", "remediation": "更新有效资质后重新提交"}


def test_classifier_labels():
    c = RuleClassifier()
    assert c.classify("好", "REASON", CTX).label == "affirmative"
    assert c.classify("不是本人", "IDENTITY_CONFIRM", CTX).label == "deny_identity"
    assert c.classify("你叫啥", "IDENTITY_CONFIRM", CTX).label == "question_identity"
    assert c.classify("今天天气不错", "REASON", CTX).label == "other"


def test_hit_outputs_candidate_and_prepares_next_round():
    pipeline = create_pipeline()
    pipeline.prepare("REASON", CTX)
    decision = pipeline.handle("好", "REASON", CTX)
    assert decision.hit is True
    assert decision.filler_used is False
    assert decision.candidate is not None
    assert decision.next_stage == "REMEDIATION"
    assert "主体资质到期" in decision.text
    assert decision.output_classification is not None
    assert any(c.stage == "REMEDIATION" for c in decision.prepared_candidates)


def test_miss_uses_classifier_selected_filler():
    pipeline = create_pipeline()
    pipeline.prepare("REASON", CTX)
    decision = pipeline.handle("今天天气不错", "REASON", CTX)
    assert decision.hit is False
    assert decision.filler_used is True
    assert decision.candidate is not None
    assert decision.candidate.source == "filler"
    assert decision.needs_llm is True
    assert decision.text
    assert decision.prepared_candidates


def test_arbitrary_input_always_gets_pre_generated_filler():
    pipeline = create_pipeline()
    decision = pipeline.handle("asdf", "REASON", CTX)
    assert decision.hit is False
    assert decision.filler_used is True
    assert decision.needs_llm is True
    assert decision.text
    assert decision.candidate is not None
    assert decision.candidate.source == "filler"


def test_identity_question_is_not_treated_as_affirmative():
    pipeline = create_pipeline()
    pipeline.prepare("CONFIRM_ACTION", CTX)
    decision = pipeline.handle("你叫啥", "CONFIRM_ACTION", CTX)
    assert decision.hit is True
    assert "京东" in decision.text
    assert "登记" not in decision.text


def test_candidate_store_caches_by_stage():
    store = InMemoryCandidateStore(PresetCandidateFactory())
    first = store.prepare("REASON", CTX)
    second = store.prepare("REASON", CTX)
    assert first and second
    assert store.match("REASON", "affirmative") is not None
