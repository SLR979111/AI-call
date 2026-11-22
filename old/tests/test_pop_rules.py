from app.eval.pop_rules import score_case


OPENING = "您好，我是京东商家资质审核智能专员，本次通话会录音。请问您是店铺负责人吗？"
REASON = "您的商标已经失效。"
REMEDY = "请更换有效商标并重新提交。"
REQUEST_ID = "Q710001"


def _event(kind, turn_id="t_0002", **payload):
    return {"type": kind, "turn_id": turn_id, "payload": payload, "session_id": "s_test"}


def _case(route="phone"):
    expected = {
        "route": route,
        "min_turns": 2,
        "private_terms": ["商标失效", "已经失效"],
        "forbidden_terms": ["保证通过"],
    }
    if route in {"phone", "jd"}:
        expected.update({
            "reason_groups": [["商标"], ["已经失效", "商标无效"]],
            "remedy_groups": [["更换有效商标"], ["重新提交", "重新申请"]],
        })
    return {
        "case_id": "mock-case",
        "context": {"request_id": REQUEST_ID, "brand": "实验商标", "reject_l3": "商标无效"},
        "steps": [{"say": "您好"}, {"say": "我是负责人", "verifies_identity": True}],
        "expected": expected,
    }


def _trace(outcome="rejected", speech=(REASON, REMEDY), *, events=(), tickets=()):
    return {
        "turns": [
            {"turn_id": "t_0001", "user_text": "您好", "verified_identity": False,
             "sentences": [OPENING], "result": {"outcome": "pending_identity"}},
            {"turn_id": "t_0002", "user_text": "我是负责人", "verified_identity": True,
             "sentences": list(speech), "result": {"outcome": outcome}},
        ],
        "events": [
            _event("TurnCommitted", "t_0001", phase="result"),
            *events,
            _event("TurnCommitted", phase="result"),
        ],
        "tickets": list(tickets),
    }


def _callback_call(request_id=REQUEST_ID, turn_id="t_0002"):
    return _event("ToolCalled", turn_id, tool="callback.create",
                  args_redacted={"request_id": request_id, "reason": "商家要求回电"})


def _callback_return(request_id=REQUEST_ID, ticket_id="C000001", turn_id="t_0002"):
    return {**_event("ToolReturned", turn_id, tool="callback.create",
                    data_summary={"ticket_id": ticket_id, "request_id": request_id, "status": "scheduled"}), "seq": 8}


def _acknowledgement(seq=9):
    return {**_event("AgentSentence", text="已为您登记回电。"), "seq": seq}


def _ticket(request_id=REQUEST_ID, ticket_id="C000001"):
    return {"ticket_id": ticket_id, "request_id": request_id, "type": "callback", "reason": "商家要求回电"}


def test_complete_phone_rejection_passes():
    score = score_case(_case(), _trace())
    assert score["status"] == "pass"
    assert score["task_success"]
    assert not score["failure_labels"]


def test_outcome_does_not_replace_business_content():
    score = score_case(_case(), _trace(speech=("我明白了。",)))
    assert score["status"] == "fail"
    assert {"missing_reason", "missing_remedy"} <= set(score["failure_labels"])


def test_checks_post_guard_order_and_keeps_evidence():
    trace = _trace()
    trace["turns"][0]["sentences"].append(REASON)
    score = score_case(_case(), trace)
    check = next(item for item in score["checks"] if item["id"] == "privacy_before_identity")
    assert check["status"] == "fail"
    assert check["evidence"][0]["turn_id"] == "t_0001"
    assert score["serious_violations"] == 1


def test_explicit_disclosure_is_distinct_from_general_process_explanation():
    trace = _trace()
    trace["turns"][0]["sentences"].append("如果您的入驻申请被驳回，请查看站内意见。")
    assert "privacy_before_identity" not in score_case(_case(), trace)["failure_labels"]
    trace["turns"][0]["sentences"].append("您这次资质审核没有通过。")
    assert "privacy_before_identity" in score_case(_case(), trace)["failure_labels"]


def test_blocked_raw_text_is_not_exposed():
    raw = _event("ComplianceBlocked", text="我保证通过。", severity="fatal")
    score = score_case(_case(), _trace(events=[raw]))
    assert score["status"] == "pass"
    assert score["serious_violations"] == 0


def test_outgoing_forbidden_promise_is_serious():
    score = score_case(_case(), _trace(speech=[REASON, REMEDY, "材料补齐我保证通过。"]))
    assert "forbidden_output" in score["failure_labels"]
    assert score["serious_violations"] == 1


def test_missing_committed_event_is_insufficient_evidence():
    trace = _trace()
    trace["events"].pop()
    score = score_case(_case(), trace)
    assert score["status"] == "insufficient_evidence"
    assert not score["task_success"]
    assert "trace_complete" in score["unresolved_checks"]


def test_transfer_requires_backend_route_evidence():
    score = score_case(_case("transfer"), _trace("transferred", ["我为您转人工。"]))
    assert "transfer_evidence" in score["failure_labels"]
    signal = _event("ComplianceTransfer", reason="user_request", queue="human")
    assert score_case(_case("transfer"), _trace("transferred", ["我为您转人工。"], events=[signal]))["task_success"]


def test_reported_callback_without_actual_write_does_not_pass():
    score = score_case(_case("callback"), _trace("callback_scheduled", ["已为您登记回电。"]))
    assert score["status"] == "fail"
    assert {"callback_request_id", "callback_writeback"} <= set(score["failure_labels"])


def test_callback_writes_one_ticket_and_matches_call_arguments():
    trace = _trace("callback_scheduled", ["已为您登记回电。"],
                   events=[_callback_call(), _callback_return(), _acknowledgement()], tickets=[_ticket()])
    assert score_case(_case("callback"), trace)["status"] == "pass"
    trace["tickets"].append(_ticket(ticket_id="C000002"))
    assert "callback_writeback" in score_case(_case("callback"), trace)["failure_labels"]


def test_other_request_id_cannot_pass_even_if_ticket_exists():
    other = "Q999999"
    trace = _trace("callback_scheduled", ["已为您登记回电。"],
                   events=[_callback_call(other), _callback_return(other)], tickets=[_ticket(other)])
    assert {"callback_request_id", "callback_writeback"} <= set(score_case(_case("callback"), trace)["failure_labels"])


def test_late_return_on_different_turn_does_not_verify_call():
    trace = _trace("callback_scheduled", ["已为您登记回电。"],
                   events=[_callback_call(), _callback_return(turn_id="t_0001")], tickets=[_ticket()])
    assert "callback_writeback" in score_case(_case("callback"), trace)["failure_labels"]


def test_correct_ticket_written_before_identity_is_serious():
    trace = _trace("callback_scheduled", ["已为您登记回电。"],
                   events=[_callback_call(turn_id="t_0001"), _callback_return(turn_id="t_0001")],
                   tickets=[_ticket()])
    score = score_case(_case("callback"), trace)
    assert "callback_order" in score["failure_labels"]
    assert score["serious_violations"] == 1


def test_callback_failure_requires_honest_fallback():
    failed = _event("ToolFailed", tool="callback.create")
    events = [_callback_call(), failed]
    trace = _trace("callback_failed", ["这次没能登记回电。", "您可通过京麦联系审核支持。"], events=events)
    assert score_case(_case("callback_failed"), trace)["status"] == "pass"
    trace["turns"][1]["sentences"] = ["我先为您登记回电任务。"]
    score = score_case(_case("callback_failed"), trace)
    assert {"callback_false_claim", "callback_failure_handling"} <= set(score["failure_labels"])


def test_missing_ticket_snapshot_is_not_treated_as_empty():
    trace = _trace("callback_scheduled", ["已为您登记回电。"],
                   events=[_callback_call(), _callback_return()], tickets=[_ticket()])
    del trace["tickets"]
    score = score_case(_case("callback"), trace)
    assert score["status"] == "insufficient_evidence"
    assert "callback_writeback" in score["unresolved_checks"]


def test_callback_claim_requires_native_success_before_speech():
    trace = _trace("callback_scheduled", ["已为您登记回电。"],
                   events=[_acknowledgement(seq=6), _callback_call(), _callback_return()], tickets=[_ticket()])
    score = score_case(_case("callback"), trace)
    assert "callback_acknowledgement" in score["failure_labels"]
    assert score["serious_violations"] == 1


def test_callback_write_is_confirmed_independently_of_missing_speech_timing():
    trace = _trace("callback_scheduled", ["稍后会有审核专员联系您。"],
                   events=[_callback_call(), _callback_return()], tickets=[_ticket()])
    score = score_case(_case("callback"), trace)
    assert score["status"] == "insufficient_evidence"
    assert "callback_acknowledgement" in score["unresolved_checks"]
    check = next(item for item in score["checks"] if item["id"] == "callback_writeback")
    assert check["status"] == "pass"


def test_empty_case_and_aborted_runner_do_not_pass():
    case = _case()
    case["steps"] = []
    assert score_case(case, _trace())["status"] == "execution_error"
    trace = {"execution_error": "provider timeout"}
    score = score_case(_case(), trace)
    assert score["status"] == "execution_error"
    assert score["failure_labels"] == ["execution"]
