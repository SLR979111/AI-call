"""Exercise the same mixed-fixture entry point used by CI."""
import json

import pytest

from contracts import validate as validator


@pytest.fixture(params=[True, False], ids=["jsonschema", "builtin"])
def engine(request, monkeypatch):
    monkeypatch.setattr(validator, "HAVE_JSONSCHEMA", request.param)


def test_all_contract_families_are_checked(engine, capsys):
    assert validator.main([]) == 0
    output = capsys.readouterr().out
    for name in ("session_normal.jsonl", "ws_session_bargein.jsonl", "predictive.v1.handle.json"):
        assert f"OK contracts/fixtures/{name}" in output
    assert "6 file(s), 91 document(s) valid" in output


@pytest.mark.parametrize("mutation", ["missing_decision", "invalid_confidence"])
def test_invalid_predictive_fixture_is_rejected(engine, tmp_path, mutation):
    fixture = validator.CONTRACTS_DIR / "fixtures" / "predictive.v1.handle.json"
    document = json.loads(fixture.read_text(encoding="utf-8"))
    if mutation == "missing_decision":
        del document["decision"]
    else:
        document["decision"]["classification"]["confidence"] = 2
    target = tmp_path / fixture.name
    target.write_text(json.dumps(document), encoding="utf-8")
    assert validator.main([str(target)]) == 1


def test_explicit_schema_override_is_honored(engine):
    fixture = validator.CONTRACTS_DIR / "fixtures" / "predictive.v1.handle.json"
    assert validator.main([str(fixture), "--schema", str(validator.DEFAULT_SCHEMA)]) == 1


def test_event_sequence_check_remains_enabled(engine, tmp_path):
    fixture = validator.CONTRACTS_DIR / "fixtures" / "session_normal.jsonl"
    first = json.loads(fixture.read_text(encoding="utf-8").splitlines()[0])
    first["seq"] = 2
    target = tmp_path / fixture.name
    target.write_text(json.dumps(first) + "\n", encoding="utf-8")
    assert validator.main([str(target)]) == 1
