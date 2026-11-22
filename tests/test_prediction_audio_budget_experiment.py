"""Integration check for the offline audio-budget experiment harness."""

from scripts.experiment_prediction_audio_budget import run_arm


async def test_experiment_arms_trade_audio_bytes_for_coverage():
    for limit in (1, 2, 3, 5):
        arm = await run_arm(limit)
        assert arm["prefetch_calls"] == limit
        assert arm["coverage"]["late_published"] == 10
        assert arm["coverage"]["late_audio"] == limit
        assert arm["fallback"]["applicable"] is True
        assert arm["fallback"]["ok"] is True
        assert arm["fallback"]["tts_calls"] == 1


async def test_experiment_zero_limit_is_text_only_and_still_falls_back():
    arm = await run_arm(0)
    assert arm["prefetch_calls"] == 0
    assert arm["audio_bytes_total"] == 0
    assert arm["coverage"]["late_audio"] == 0
    assert arm["coverage"]["late_published"] == 10
    assert arm["fallback"]["ok"] is True
