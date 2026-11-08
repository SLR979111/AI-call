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
from app.agent.pop_grounded_answer import BASE_ADVICE, GroundedAnswer
from app.agent.pop_local_topics import MODEL_BUDGET_MS, PROMPT_C, LocalTopics
from app.agent.pop_repair import BASELINE, CONTEXTUAL, FAST, GUIDED, REPAIRED, RESILIENT, SemanticTopics
from app.agent.pop_resilient import PRIME_REPLIES, masked_shop
from app.agent.pop_scene_followup import load_scene_policy
from app.eval.pop_judge import JudgeConfig
from app.tools.pop_writeback import PopContactStore
from app.voice.primed import PrimedTts
from probe_pop_voice import RATE, write_wav

TURN_SAMPLES = {
    "turn-2": "对啊。", "turn-2-repeat": "对对。", "turn-2-denied": "我不是负责人。",
    "turn-3": "可以说。", "turn-4": "清楚了。",
}
PHONE_CASES = {
    "pop-scene-034": "公司主体 · 主体资质到期",
    "pop-scene-035": "公司主体 · 主体名称不符",
    "pop-scene-061": "公司主体 · 法人证件信息错误",
    "pop-scene-007": "品牌授权 · 商标持有人不一致",
    "pop-scene-009": "品牌授权 · 授权到期",
    "pop-scene-039": "品牌授权 · 授权链路不完整",
    "pop-scene-043": "品牌授权 · 授权方联系不上",
    "pop-scene-023": "新增产品线 · 图形商标",
    "pop-scene-027": "全球购 · 商标注册证为境内",
    "pop-scene-054": "行业资质 · 资质与类目不符",
}
CONTEST_SILENCE_MS = 900


def build_app(args):
    policy = {"legacy": BASELINE, "repair": REPAIRED, "guided": GUIDED,
              "contextual": CONTEXTUAL, "fast": FAST, "resilient": RESILIENT}[args.dialogue_version]
    scene_policy = (load_scene_policy(ROOT, getattr(args, "scene_policy", None))
                    if policy.resilient_dialogue and not getattr(args, "no_scene_policy", False) else None)
    semantic_config = JudgeConfig.from_env() if args.semantic_followups else None
    answer_advice = args.answer_prompt.read_text(encoding="utf-8").strip() if args.answer_prompt else BASE_ADVICE
    if not 5 <= len(answer_advice) <= 350:
        raise ValueError("Reply advice must contain 5–350 characters")
    inputs = ("scripts/serve_pop_contest.py", "knowledge/pop_audit.json",
              "app/agent/pop_competition.py", "app/agent/pop_repair.py", "app/agent/pop_guidance.py",
              "app/tools/pop_writeback.py",
              "app/main.py", "app/orchestrator.py", "web/rtc/src/session.js", "web/rtc/src/main.js",
              "web/rtc/src/protocol.js", "web/rtc/src/ui.js", "web/rtc/src/vad-local.js", "web/rtc/index.html",
              "app/voice/factory.py", "app/voice/sherpa_asr.py", "app/voice/melo.py")
    artifacts = b"".join((ROOT / path).read_bytes() for path in inputs)
    artifacts += f"{policy.version}|{args.tts_speed}|{semantic_config and semantic_config.model}".encode()
    if policy.contextual_followups:
        artifacts += (ROOT / "app/agent/pop_contextual.py").read_bytes()
    if policy.fast_followups:
        artifacts += (ROOT / "app/agent/pop_fast_reply.py").read_bytes()
    if policy.resilient_dialogue:
        artifacts += (ROOT / "app/agent/pop_resilient.py").read_bytes()
    if scene_policy:
        artifacts += (ROOT / "app/agent/pop_scene_followup.py").read_bytes() + scene_policy.version.encode()
    if args.local_followups_port:
        artifacts += (ROOT / "app/agent/pop_local_topics.py").read_bytes()
        artifacts += f"|local:{MODEL_BUDGET_MS}".encode()
    if args.local_answer_port:
        artifacts += (ROOT / "app/agent/pop_grounded_answer.py").read_bytes() + answer_advice.encode()
    if args.prime_tts:
        artifacts += (ROOT / "app/voice/primed.py").read_bytes()
    digest = hashlib.sha256(artifacts).hexdigest()[:12]
    release = f"pop-contest-resilient-{digest}" if policy.resilient_dialogue else f"pop-contest-{digest}"
    os.environ.update(
        APP_ENV="contest_demo", DEV_NO_AUTH="0", AGENT_PROVIDER="fake", SCENARIO_ID="pop_qualification_audit",
        DATA_DIR=str(args.output), RELEASE_ID=release,
        ASR_PROVIDER="sherpa", TTS_PROVIDER="melo", TTS_FALLBACK_PROVIDER="",
        ASR_MODEL_DIR=str(args.asr_model), MELO_MODEL_DIR=str(args.tts_model),
        MELO_SPEED=str(args.tts_speed),
    )
    from app.main import app, lifespan
    app.state.rtc_pcm_enabled = args.rtc_pcm
    print(f"POP dialogue={args.dialogue_version} release={release} "
          f"model={'local-answer' if args.local_answer_port else 'local-topics' if args.local_followups_port else 'cloud-topics' if args.semantic_followups else 'off'}",
          f" scene_policy={scene_policy.version if scene_policy else 'off'}", flush=True)
    if args.rtc_pcm:
        try:
            import aiortc  # noqa: F401
        except ImportError as exc:
            raise SystemExit(
                "--rtc-pcm requires the optional WebRTC dependency; "
                "install it with: python -m pip install -r requirements-rtc.txt"
            ) from exc

    documents = {doc["id"]: doc for doc in read_json(KNOWLEDGE)}
    labels = dict(PHONE_CASES)
    if args.doc_id not in labels:
        labels[args.doc_id] = " ".join(documents.get(args.doc_id, {}).get("title", "").split())
    case_contexts = {}
    for case_id in labels:
        scene = documents.get(case_id, {}).get("scene", {})
        if scene.get("mode") != "电话" or not scene.get("l3"):
            raise ValueError(f"Select a synthetic phone-handling scene: {case_id}")
        case_contexts[case_id] = {
            "doc_id": case_id, "brand": args.brand,
            "request_id": args.request_id if case_id == args.doc_id else f"Q984{case_id.rsplit('-', 1)[-1]}",
        }
    app.state.contest_catalog = {
        "default_case_id": args.doc_id, "silence_ms": CONTEST_SILENCE_MS,
        "cases": [{"id": case_id, "label": label} for case_id, label in labels.items()],
    }

    @asynccontextmanager
    async def contest_lifespan(app):
        import httpx

        async with lifespan(app), httpx.AsyncClient(
            follow_redirects=False, trust_env=not (args.local_followups_port or args.local_answer_port),
        ) as client:
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
                if args.local_followups_port:
                    semantic = LocalTopics(client, args.local_followups_port, prompt=PROMPT_C, constrained=True)
                generator = (GroundedAnswer.local(client, args.local_answer_port, advice=answer_advice)
                             if args.local_answer_port else None)
                app.state.orchestrator.agent = PopCompetitionAgent(
                    KNOWLEDGE, case_contexts[args.doc_id], store, policy=policy,
                    semantic_topics=semantic, grounded_answer=generator, case_contexts=case_contexts,
                    scene_policy=scene_policy,
                )
                if args.prime_tts:
                    primed = PrimedTts(runtime.tts)
                    lines = PRIME_SENTENCES
                    if policy.resilient_dialogue:
                        lines += PRIME_REPLIES + (masked_shop(args.brand),)
                    await primed.prime(dict.fromkeys(lines))
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
    parser.add_argument("--tts-speed", type=float, default=0.93)
    version = parser.add_mutually_exclusive_group()
    version.add_argument("--dialogue-version", choices=("legacy", "repair", "guided", "contextual", "fast", "resilient"), default="resilient")
    version.add_argument("--baseline-dialogue", dest="dialogue_version", action="store_const", const="legacy")
    semantic = parser.add_mutually_exclusive_group()
    semantic.add_argument("--semantic-followups", action="store_true")
    semantic.add_argument("--local-followups-port", type=int, help="Port of loopback llama-server with alias pop-local")
    semantic.add_argument("--local-answer-port", type=int, help="Generate grounded replies via loopback llama-server")
    parser.add_argument("--answer-prompt", type=Path, help="Optional offline candidate reply advice")
    scene_options = parser.add_mutually_exclusive_group()
    scene_options.add_argument("--scene-policy", type=Path, help="Pin a validated offline scene routing policy")
    scene_options.add_argument("--no-scene-policy", action="store_true", help="Use the baseline without scene RSI")
    parser.add_argument("--rtc-pcm", action="store_true",
                        help="enable optional WebRTC DataChannel (install requirements-rtc.txt first)")
    parser.add_argument("--fault-writeback", action="store_true")
    args = parser.parse_args()
    if args.local_followups_port is not None and (
        args.dialogue_version not in {"guided", "contextual"} or not 1 <= args.local_followups_port <= 65535
    ):
        parser.error("--local-followups-port is the older classifier. Remove it for resilient, or choose --dialogue-version guided explicitly.")
    if args.local_answer_port is not None and (
        args.dialogue_version not in {"contextual", "fast", "resilient"} or not 1 <= args.local_answer_port <= 65535
    ):
        parser.error("--local-answer-port requires contextual, fast or resilient dialogue and a valid TCP port")
    if args.answer_prompt and args.local_answer_port is None:
        parser.error("--answer-prompt requires --local-answer-port")
    if args.scene_policy and args.dialogue_version != "resilient":
        parser.error("--scene-policy requires resilient dialogue")
    args.output.mkdir(parents=True, exist_ok=False)
    with os.add_dll_directory(str(args.dll_dir.resolve())) if args.dll_dir else nullcontext():
        if args.dll_dir:
            ctypes.WinDLL(str(args.dll_dir.resolve() / "onnxruntime.dll"))
        uvicorn.run(build_app(args), host="127.0.0.1", port=args.port, access_log=False)
