"""Launch the six-stage POP demo on loopback with a synthetic merchant record."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import os
import shutil
import sys
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.cancel import CancelToken
from app.eval.pop_runner import KNOWLEDGE, read_json
from app.agent.pop_competition import PRIME_SENTENCES, PopCompetitionAgent
from app.agent.pop_guidance import GUIDANCE_PROMPT, GUIDANCE_TOPICS
from app.agent.pop_repair import BASELINE, GUIDED, REPAIRED, SemanticTopics
from app.eval.pop_judge import JudgeConfig
from app.tools.pop_writeback import PopContactStore
from app.voice.primed import PrimedTts
from probe_pop_voice import RATE, write_wav

TURN_SAMPLES = {
    "turn-2": "对啊。", "turn-2-repeat": "对对。", "turn-2-denied": "我不是负责人。",
    "turn-3": "可以说。", "turn-4": "清楚了。",
}


def build_app(args):
    policy = {"legacy": BASELINE, "repair": REPAIRED, "guided": GUIDED}[args.dialogue_version]
    semantic_config = JudgeConfig.from_env() if args.semantic_followups else None
    inputs = ("app/agent/pop_competition.py", "app/agent/pop_repair.py", "app/agent/pop_guidance.py",
              "app/tools/pop_writeback.py",
              "app/main.py", "app/orchestrator.py", "web/rtc/src/session.js", "web/rtc/src/main.js",
              "web/rtc/src/protocol.js", "app/voice/melo.py")
    artifacts = b"".join((ROOT / path).read_bytes() for path in inputs)
    artifacts += f"{policy.version}|{args.tts_speed}|{semantic_config and semantic_config.model}".encode()
    if args.prime_tts:
        artifacts += (ROOT / "app/voice/primed.py").read_bytes()
    digest = hashlib.sha256(artifacts).hexdigest()[:12]
    os.environ.update(
        APP_ENV="contest_demo", DEV_NO_AUTH="0", AGENT_PROVIDER="fake", SCENARIO_ID="pop_qualification_audit",
        DATA_DIR=str(args.output), RELEASE_ID=f"pop-contest-{digest}",
        ASR_PROVIDER="sherpa", TTS_PROVIDER="melo", TTS_FALLBACK_PROVIDER="",
        ASR_MODEL_DIR=str(args.asr_model), MELO_MODEL_DIR=str(args.tts_model),
        MELO_SPEED=str(args.tts_speed),
    )
    from app.main import app, lifespan
    app.state.rtc_pcm_enabled = args.rtc_pcm
    if args.rtc_pcm:
        import aiortc  # Verify the optional transport dependency at startup.

    scene = next(doc["scene"] for doc in read_json(KNOWLEDGE) if doc["id"] == args.doc_id)
    if scene["mode"] != "电话":
        raise ValueError("Select a synthetic phone-handling scene")
    context = {"request_id": args.request_id, "brand": args.brand,
               **{f"reject_{key}": scene[key] for key in ("l1", "l2", "l3")}}

    @asynccontextmanager
    async def contest_lifespan(app):
        import httpx

        async with lifespan(app), httpx.AsyncClient(follow_redirects=False) as client:
            runtime = app.state.voice
            if runtime.asr is None or runtime.tts is None:
                raise RuntimeError((await runtime.refresh()).detail)
            runtime.asr.finish_padding_ms = 500
            store = PopContactStore(args.output / "index/pop_contacts.db", app.state.bus,
                                    fault=args.fault_writeback)
            try:
                semantic_options = ({"prompt": GUIDANCE_PROMPT, "allowed_topics": GUIDANCE_TOPICS}
                                    if policy.guided_remediation else {})
                semantic = (SemanticTopics(client, semantic_config.endpoint, semantic_config.model,
                                           semantic_config.api_key, **semantic_options) if semantic_config else None)
                app.state.orchestrator.agent = PopCompetitionAgent(KNOWLEDGE, context, store, policy=policy,
                                                                   semantic_topics=semantic)
                if args.prime_tts:
                    primed = PrimedTts(runtime.tts)
                    await primed.prime(PRIME_SENTENCES)
                    app.state.orchestrator.tts = primed
                if args.reuse_audio:
                    shutil.copytree(args.reuse_audio, args.output / "input_audio")
                elif args.prepare_audio:
                    audio = args.output / "input_audio"
                    audio.mkdir(exist_ok=True)
                    for name, phrase in TURN_SAMPLES.items():
                        chunks = [chunk.pcm async for chunk in runtime.tts.synthesize(
                            phrase, sample_rate=RATE, cancel=CancelToken(),
                        )]
                        write_wav(audio / f"{name}.wav", b"".join(chunks), RATE)
                yield
            finally:
                store.close()

    app.router.lifespan_context = contest_lifespan
    return app


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--asr-model", type=Path, required=True)
    parser.add_argument("--tts-model", type=Path, required=True)
    parser.add_argument("--dll-dir", type=Path)
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--doc-id", default="pop-scene-034")
    parser.add_argument("--request-id", default="Q983001")
    parser.add_argument("--brand", default="演示松潮生活")
    parser.add_argument("--prepare-audio", action="store_true")
    parser.add_argument("--reuse-audio", type=Path)
    parser.add_argument("--prime-tts", action="store_true")
    parser.add_argument("--tts-speed", type=float, default=1.0)
    version = parser.add_mutually_exclusive_group()
    version.add_argument("--dialogue-version", choices=("legacy", "repair", "guided"), default="guided")
    version.add_argument("--baseline-dialogue", dest="dialogue_version", action="store_const", const="legacy")
    parser.add_argument("--semantic-followups", action="store_true")
    parser.add_argument("--rtc-pcm", action="store_true")
    parser.add_argument("--fault-writeback", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    with os.add_dll_directory(str(args.dll_dir.resolve())) if args.dll_dir else nullcontext():
        if args.dll_dir:
            ctypes.WinDLL(str(args.dll_dir.resolve() / "onnxruntime.dll"))
        uvicorn.run(build_app(args), host="127.0.0.1", port=args.port, access_log=False)
