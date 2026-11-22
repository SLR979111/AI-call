"""Local SenseVoice transcription and its cloud fallback."""
import asyncio

from app.local_asr import LocalAsr, load_from_env, make_transcriber


class Cloud:
    def __init__(self, text="云端结果"):
        self.calls = 0
        self.text = text

    async def transcribe(self, pcm):
        self.calls += 1
        return self.text


class Local:
    def __init__(self, text="本地结果", ready=True, error=None):
        self.ready, self.text, self.error = ready, text, error
        self.calls = 0

    async def transcribe(self, pcm):
        self.calls += 1
        if self.error:
            raise self.error
        return self.text


def run(coro):
    return asyncio.run(coro)


def test_local_model_wins_when_it_returns_text():
    cloud, local = Cloud(), Local()
    text = run(make_transcriber(local, cloud)(b"\x00\x00" * 320))
    assert text == "本地结果" and cloud.calls == 0


def test_empty_local_text_falls_back_to_cloud():
    cloud, local = Cloud(), Local(text="")
    text = run(make_transcriber(local, cloud)(b"\x00\x00" * 320))
    assert text == "云端结果" and local.calls == 1 and cloud.calls == 1


def test_local_failure_falls_back_to_cloud():
    cloud, local = Cloud(), Local(error=RuntimeError("onnx"))
    assert run(make_transcriber(local, cloud)(b"\x00\x00" * 320)) == "云端结果"


def test_missing_local_model_is_skipped():
    cloud = Cloud()
    assert run(make_transcriber(None, cloud)(b"\x00\x00" * 320)) == "云端结果"


def test_load_from_env_can_be_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_ASR", "0")
    assert load_from_env() is None


def test_load_from_env_requires_model_files(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_ASR", "1")
    monkeypatch.setenv("LOCAL_ASR_DIR", str(tmp_path))
    assert load_from_env() is None
    (tmp_path / "tokens.txt").write_text("a\n")
    assert isinstance(load_from_env(), LocalAsr)

def test_non_native_sample_rate_uses_locked_resampler():
    from unittest.mock import Mock
    asr = LocalAsr()
    recognizer = Mock()
    asr._recognizer = recognizer
    stream = recognizer.create_stream.return_value
    stream.result.text = "词"
    assert asr.transcribe_sync(bytes(4800 * 2), sample_rate=48000) == "词"
    rate, samples = stream.accept_waveform.call_args.args
    assert rate == 16000 and len(samples) == 1600
