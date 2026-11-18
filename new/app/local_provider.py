"""Offline ASR and TTS; only a loopback Ollama server receives dialogue."""

import asyncio
import json
import re
from contextlib import aclosing
from concurrent.futures import ThreadPoolExecutor

import httpx
import numpy as np

from .forecast import match_example
from .local_asr import install_model, model_dir
from .models import Say, Update
from .providers import Cloud, ProviderError, Settings
from .speech_start import first_sound
from .voice_preview import first_clause

VOICE = "vits-piper-zh_CN-huayan-medium"
MODEL = "qwen2.5:7b-instruct"
VOICE_FILES = ("zh_CN-huayan-medium.onnx", "zh_CN-huayan-medium.onnx.json", "MODEL_CARD")
INSTALL_HELP = "请先安装 Ollama 并运行 scripts/setup-local.ps1，然后重试。"


def install_voice():
    install_model("tts", VOICE, "tts-models",
                  "dbdfec42b91d9cee31cce9ff4b3e9c305eb6fbf60546d071f7e46273554cce6b",
                  VOICE_FILES)


def load_voice():
    from piper import PiperVoice

    root = model_dir("tts", VOICE)
    if not all((root / name).is_file() for name in VOICE_FILES):
        raise ProviderError("尚未安装本地中文音色。" + INSTALL_HELP)
    return PiperVoice.load(root / VOICE_FILES[0])


class LocalProvider(Cloud):
    is_local = True

    def __init__(self, recognizer):
        client = httpx.AsyncClient(base_url="http://127.0.0.1:11434", trust_env=False,
                                   follow_redirects=False, timeout=httpx.Timeout(75, connect=2))
        super().__init__(Settings(api_key="", base_url="http://127.0.0.1:11434", model=MODEL, voice=VOICE), client)
        self.recognizer = recognizer
        self.engine = None
        self.ready_lock = asyncio.Lock()
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="offline-voice")

    async def ready(self, voice=True):
        async with self.ready_lock:
            if voice and self.recognizer is None:
                raise ProviderError("尚未安装本地语音识别模型。" + INSTALL_HELP)
            try:
                info = await self.client.post("/api/show", json={"model": MODEL})
                if info.status_code != 200:
                    raise ProviderError("本机缺少对话模型。" + INSTALL_HELP)
                metadata = info.json()
                if metadata.get("remote_model") or metadata.get("remote_host") or not metadata.get("model_info"):
                    raise ProviderError("纯本地模式需要已下载到本机的对话模型。")
                warm = await self.client.post("/api/generate", json={"model": MODEL, "stream": False,
                                              "keep_alive": "15m", "options": {"num_ctx": 8192}})
                warm.raise_for_status()
            except httpx.HTTPError as exc:
                raise ProviderError("本机 Ollama 服务不可用。" + INSTALL_HELP) from exc
            if voice and self.engine is None:
                self.engine = await asyncio.get_running_loop().run_in_executor(self.worker, load_voice)

    async def chat(self, messages, *, max_tokens=650, schema=None):
        payload = {"model": MODEL, "messages": messages, "stream": True, "think": False, "keep_alive": "15m",
                   "options": {"temperature": 0.2, "num_predict": max_tokens, "num_ctx": 8192}}
        if schema is not None:
            payload["format"] = schema
        async with self.client.stream("POST", "/api/chat", json=payload) as response:
            self.check(response, "本机对话模型")
            async for line in response.aiter_lines():
                if line:
                    item = json.loads(line)
                    if item.get("error") or item.get("done_reason") == "length":
                        raise ProviderError("本机模型未能完成回复。")
                    piece = item.get("message", {}).get("content")
                    if piece:
                        yield piece

    async def structured(self, model, instruction, context, max_tokens, on_first_clause=None):
        schema = model.model_json_schema()
        messages = [{"role": "system", "content": instruction}]
        if model is Say:
            # Keep task instructions out of the caller's role. A small local
            # model must distinguish actual statements from conditional examples.
            task = context["task"]
            facts = task["facts"]
            sections = [instruction, "任务：" + task["goal"],
                        "检查项：" + json.dumps(task["checkpoints"], ensure_ascii=False),
                        "会话进度：" + json.dumps(context["state"], ensure_ascii=False),
                        "任务原文：\n" + facts,
                        "请按已知记录回答。除非用户明确变更情况，否则沿用记录中已经发生的情况。"]
            # Some cards explicitly label their current facts alongside hypothetical guidance.
            # Keep that source visible near the caller's turn without assuming a card format.
            current = [line for line in facts.splitlines()
                       if line.lstrip().startswith(("【这次", "【已知", "【当前", "【目前", "【现状", "【已有", "【现有", "【本次"))]
            if current:
                sections.append("对方尚未报告变更时，采用以下现状作为起点：\n" + "\n".join(current))
            cutoffs = [e for e in context["dialogue"][-8:] if "remaining_text" in e]
            if cutoffs:
                sections.append("打断记录：" + json.dumps(cutoffs, ensure_ascii=False))
            messages[0]["content"] = "\n".join(sections)
            for entry in context["dialogue"]:
                if entry.get("delivery") == "unplayed" or entry.get("filler") or entry.get("bridge"):
                    continue
                if entry.get("text"):
                    messages.append({"role": entry["role"], "content": entry["text"]})
            points = [p["id"] for p in context["task"]["checkpoints"]]
            del schema["properties"]["question"]
            schema["properties"]["covers"]["items"] = {"type": "string", "enum": points}
            schema["properties"]["text"]["maxLength"] = 100
        else:
            messages.append({"role": "user", "content": json.dumps(context, ensure_ascii=False)})
        for obj in [schema, *schema.get("$defs", {}).values()]:
            if obj.get("type") == "object":
                obj["additionalProperties"] = False
                obj["required"] = list(obj["properties"])
        raw = ""
        async with aclosing(self.chat(messages, max_tokens=max_tokens, schema=schema)) as stream:
            async for chunk in stream:
                raw += chunk
                obj = raw.lstrip()
                if on_first_clause and obj.startswith("{"):
                    ready = first_clause('{"type":"say",' + obj[1:])
                    if ready:
                        on_first_clause(ready)
                        on_first_clause = None
                if raw.rstrip().endswith("}"):
                    try:
                        value = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    return model.model_validate(value)
        return model.model_validate_json(raw)

    async def respond_local(self, snapshot, candidates, on_first_clause=None):
        latest = snapshot["dialogue"][-1]["text"]
        previous = next((e for e in reversed(snapshot["dialogue"][:-1]) if e["role"] == "assistant"), {})
        anchor = previous.get("text", "") if previous.get("delivery") == "complete" else ""
        match = match_example(latest, candidates, anchor)
        if match:
            line = match.reply
            yield "reuse", line
        else:
            line = await self.structured(Say,
                "你是中文电话客服。优先处理最后一句中的新情况和具体问题。"
                "只基于本轮用户原话和此前已经说过的话判断当前情况。资料中含有‘若/如果/可能/后来’的内容仅是条件，禁止把这些情况当作用户已报告的事实。"
                "text 只用一句话先回答眼前的一件事，通常15至35个字。只确认负责或方便时就说明现有记录里的第一个问题。"
                "不要用‘您提到/您已经’引出对方没说过的事，不复述整套流程，不加例行反问。"
                "只用给出的事实。对方明确改了做法时，回答新做法下的要求。没提供的信息就说无法确认。不要说明推理，不承诺查询或回电。"
                "打断历史的 text 是已听到的部分。忙或拒绝时尊重对方。"
                "covers 只填本句完整讲清的 checkpoint id。",
                snapshot, 240, on_first_clause)
            if len(line.text) >= 98 and line.text[-1] not in "。！？!?":
                end = max(line.text.rfind(mark) for mark in "。！？!?")
                if end >= 5:
                    line.text = line.text[:end + 1]
                    line.covers = []
            valid = {p["id"] for p in snapshot["task"]["checkpoints"]}
            line.covers = [point for point in line.covers if point in valid]
            question = re.search(r"[^。！？?]*[？?]$", line.text.strip())
            line.question = question[0] if question else ""
            yield "say", line
        update = await self.structured(Update,
            "根据任务对话、本轮用户原话和本轮回复更新状态。facts 只记录用户明确说过的事，quote 须逐字抄原话。"
            "questions 只留本轮仍未回答的问题。若此前已告知全部要点、用户明确表示理解且无待答问题，用 complete 并提供 confirmation 原话。"
            "忙用 paused，拒绝用 declined，暂停拒绝时 disposition_quote 摘自用户原话。其他情况用 active。",
            {**snapshot, "reply": line.model_dump()}, 420)
        yield "update", update

    async def transcribe(self, pcm):
        return await self.recognizer.transcribe(pcm)

    def render(self, text):
        pieces = []
        for chunk in self.engine.synthesize(text):
            samples = np.frombuffer(chunk.audio_int16_bytes, dtype="<i2")
            times = np.arange(round(len(samples) * 24000 / chunk.sample_rate)) * (chunk.sample_rate / 24000)
            pieces.append(np.interp(times, np.arange(len(samples)), samples).astype("<i2").tobytes())
        pcm = b"".join(pieces)
        if not pcm:
            raise ProviderError("本地合成未返回语音。")
        return pcm[first_sound(pcm) or 0:]

    async def speech(self, text):
        pcm = self.cached(text)
        if pcm is None:
            pcm = await asyncio.get_running_loop().run_in_executor(self.worker, self.render, text)
            self.remember(text, pcm)
        for offset in range(0, len(pcm), 1920):
            yield pcm[offset:offset + 1920]

    async def close(self):
        await self.client.aclose()
        self.worker.shutdown(wait=True, cancel_futures=True)


if __name__ == "__main__":
    from .local_asr import install
    install()
    install_voice()
