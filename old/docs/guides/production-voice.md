# 真实语音运行指南

默认 `ASR_PROVIDER=sherpa`、`TTS_PROVIDER=qwen3`。CPU 主应用运行流式 Zipformer；Qwen 在独立 GPU sidecar 中运行。`TTS_PROVIDER=melo` 显式切换到 CPU Melo，默认路径不导入/加载 Melo。两个 provider 都发送真实 PCM16LE，绝不自动换声音。

## 1. 模型与参考音频

运行时不下载模型。部署前通过受控的构建/准备步骤取得完整模型目录，运行时只读挂载；权重、参考音频、密钥不要提交。应用不会写这些目录。

| 配置 | 必需内容 |
|---|---|
| `ASR_MODEL_DIR` | `sherpa-onnx-streaming-zipformer-zh-int8-2025-06-30`：`encoder.int8.onnx`、`decoder.onnx`、`joiner.int8.onnx`、`tokens.txt` |
| `MELO_MODEL_DIR`（仅 Melo） | `vits-melo-tts-zh_en`：`model.onnx`、`tokens.txt`、`lexicon.txt`、`dict/`、`phone.fst`、`date.fst`、`number.fst`、`new_heteronym.fst` |
| `QWEN_MODEL_DIR`（仅 sidecar） | 完整 `Qwen3-TTS-12Hz-1.7B-Base`，包括配置、权重、文本 tokenizer、speech tokenizer；不能只挂 safetensors |
| `QWEN_REF_AUDIO` / `QWEN_REF_TEXT` | 服务端固定的参考 WAV 和准确转写；请求只允许 `text`、`sample_rate` |

产品音色必须有录音本人明确同意/相应授权，并在部署记录中保存授权凭据。确认后设置 `QWEN_REFERENCE_CONSENT=1`。仓库不带参考音频。brief 中的 `zero_shot_prompt.wav` 仅限本地试听，不可据此宣称产品音色获授权。本地试听可以显式设置 `APP_ENV=dev QWEN_REFERENCE_MODE=audition`；健康检查会标注试听，`APP_ENV=prod` 禁止以此绕过授权确认。

## 2. 本地运行

Python 3.12；主应用环境完全不需要 torch：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:ASR_PROVIDER='sherpa'
$env:ASR_MODEL_DIR='D:\models\sherpa-onnx-streaming-zipformer-zh-int8-2025-06-30'
```

Qwen sidecar 使用独立环境（NVIDIA CUDA 12.8 兼容驱动，实测 RTX 5070 Ti Laptop GPU；显存需求需要在目标卡上验证）：

```powershell
python -m venv .venv-qwen
.venv-qwen\Scripts\python.exe -m pip install torch==2.11.0 torchaudio==2.11.0 --index-url https://download.pytorch.org/whl/cu128
.venv-qwen\Scripts\python.exe -m pip install -r services/qwen_tts/requirements.txt
$env:APP_ENV='prod'
$env:QWEN_MODEL_DIR='D:\models\Qwen3-TTS-12Hz-1.7B-Base'
$env:QWEN_REF_AUDIO='D:\authorized-voice\reference.wav'
$env:QWEN_REF_TEXT='这里填写参考录音的准确转写。'
$env:QWEN_REFERENCE_CONSENT='1'
$env:HF_HUB_OFFLINE='1'
$env:TRANSFORMERS_OFFLINE='1'
.venv-qwen\Scripts\python.exe -m uvicorn services.qwen_tts.server:app --host 127.0.0.1 --port 8090
```

另一个终端在仓库根目录启动应用，配置 ASR 路径：

```powershell
$env:ASR_PROVIDER='sherpa'
$env:ASR_MODEL_DIR='D:\models\sherpa-onnx-streaming-zipformer-zh-int8-2025-06-30'
$env:TTS_PROVIDER='qwen3'
$env:QWEN_TTS_URL='http://127.0.0.1:8090'
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8081
```

CPU-only：无需 sidecar，不改代码，设置以下变量后运行同一主应用命令：

```powershell
$env:TTS_PROVIDER='melo'
$env:MELO_MODEL_DIR='D:\models\vits-melo-tts-zh_en'
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8081
```

打开 `http://127.0.0.1:8081/rtc/`，使用麦克风、无麦回灌或文本输入。推荐通话台 `/rtc/`；旧根页面仍是早期浏览器语音演示。

## 3. Docker Compose

复制 `config/app.example` 中需要的配置到 `.env`。Compose 使用宿主路径变量：

```dotenv
ASR_MODEL_HOST_DIR=/srv/models/sherpa-onnx-streaming-zipformer-zh-int8-2025-06-30
QWEN_MODEL_HOST_DIR=/srv/models/Qwen3-TTS-12Hz-1.7B-Base
QWEN_VOICE_HOST_DIR=/srv/authorized-voice
QWEN_REF_TEXT=这里填写准确转写。
QWEN_REFERENCE_CONSENT=1
TTS_PROVIDER=qwen3
```

参考目录中音频文件固定名为 `reference.wav`。Compose 自动使用内部 `http://qwen-tts:8090`；不要把本地示例中的 localhost `QWEN_TTS_URL` 复制到 Compose，除非明确使用外部 sidecar。

```bash
docker compose --profile gpu config --quiet
docker compose --profile gpu up -d --build
curl http://localhost:8081/voice/status
docker compose --profile gpu logs -f qwen-tts app
```

GPU 机器需 NVIDIA Container Toolkit。sidecar 仅暴露内部端口，不向宿主发布，不应直接暴露互联网。运行 **一个 uvicorn worker**；多个 worker 会重复占用显存。sidecar 启动后加载、CUDA graph warmup、ICL 参考编码和实际流式 warmup 全完成才 ready。模型加载失败返回健康检查 503，保留错误原因供修复。

CPU-only `.env` 设 `TTS_PROVIDER=melo`、`MELO_MODEL_HOST_DIR=/srv/models/vits-melo-tts-zh_en`，运行 `docker compose up -d --build app`。不启用 `gpu` profile；无须 Qwen 权重或参考音频。Qwen 模式下 Melo 的默认空挂载目录无需模型文件，也不会加载模型。

`/healthz` 是应用存活探针；**语音 readiness 用 `/voice/status`**，分别检查 `asr.ready`、`tts.ready`。Qwen 的 readiness 每次查询都会请求 sidecar `/healthz`，包含超时/连接错误；不把“安装了包”当作 ready。繁忙状态见 sidecar `busy`，并发生成返回 `TTS_BUSY`。当前单模型单生成槽位，无排队/重试/自动切换；编排策略可按明确错误重试或转人工。

## 4. 配置与行为

| 变量 | 默认 | 说明 |
|---|---|---|
| `ASR_PROVIDER` | `sherpa` | `sherpa` / `fake` |
| `TTS_PROVIDER` | `qwen3` | `qwen3` / `melo` / `fake` |
| `VOICE_PROVIDER` | 未设置 | 旧变量，仅对应的新变量未设置时生效；`sherpa(-onnx)` 映射 sherpa + melo |
| `TTS_FALLBACK_PROVIDER` | 空 | 非空配置报错，不静默 fallback |
| `QWEN_TTS_URL` | `http://127.0.0.1:8090` | base URL，也支持 HTTPS/WSS |
| `QWEN_TTS_TIMEOUT_S` | `30` | 客户端每次等待音频/控制消息的超时 |
| `QWEN_CHUNK_SIZE` | `4` | 每块 codec steps，服务端可覆盖，范围 1..48 |
| `QWEN_NON_STREAMING_MODE` | `false` | 与选定试听样本一致，逐步馈入文本；接受 true/false、1/0、yes/no、on/off |
| `QWEN_TEMPERATURE` | `0.9` | 与选定样本一致，范围 (0, 2] |
| `QWEN_TOP_K` | `50` | 与选定样本一致，范围 1..200 |
| `QWEN_TOP_P` | `1.0` | 与选定样本一致，范围 (0, 1] |
| `QWEN_MAX_NEW_TOKENS` | `2048` | 单次生成 codec token 预算，范围 1..8192；静态 KV cache 仍有独立上限，调大不保证能生成更长文本 |
| `QWEN_GROUP_SENTENCES` | `3` | 首句立即合成，后续最多 3 句一组；可改 1/2/3；final/end 时冲刷尾组 |
| `QWEN_INSTRUCT` | `用自然、平稳、亲切的客服语气，语速适中，句间停顿自然，不要夸张。` | 服务端固定客服 instruct；请求不可覆盖 |

Qwen 固定 Base + `xvec_only=False`（ICL）+ 24 kHz；默认 `non_streaming_mode=False`、`temperature=0.9/top_k=50/top_p=1.0/chunk_size=4` 与用户选中的「clone + 客服 instruct」样本一致，不使用旧对照实验的 True/0.6/30/0.9 参数。上述环境变量仅在 sidecar 启动时读取，修改后需重启；显式覆盖生成参数后应重新试听。`xvec_only=False` 不提供切换开关，参考音频及转写也不通过请求替换。ready 后 sidecar `/healthz.generation` 返回实际生效参数（不包含参考路径、音频或转写），可用于部署核对。主应用只有 WS/HTTP 客户端，绝不导入 torch。

长文本按两个独立边界处理：

- 每次 sidecar 请求最多 **2000 个 Python 字符**（不是 tokenizer token）。后续多句组将超过上限时，主应用先冲刷已有组，再提交下一句，保持原句文本、句 ID 和字符偏移；正常的首句 + 最多三句策略不变。单个 Agent sentence 超过 2000 字符则明确返回 `TTS_INVALID_REQUEST`，客户端和 sidecar 也独立校验；不截断文本、不自动切碎单句改变韵律。Agent 应输出自然短句。
- codec token 预算默认 **2048**，与输入字符数没有固定换算。达到预算，或 faster-qwen3-tts 0.5.4 的静态 KV cache 最后可用位置时，返回不可自动重试的 `QWEN_OUTPUT_LIMIT`。上游库不暴露该位置是否刚好 EOS，因此 cache 边界检查宁可明确失败，也不把可能被截断的语音标成完成。已发送的 PCM 无法撤回；应用发送 `error`、`tts_completed(interrupted=true)`、错误 `turn_result`，停止该轮，不重试整段或切换 provider。即使不足 2000 字符仍可能触及生成上限，不能承诺任意 2000 字文本均能完整合成。

默认分组保留既有试听选择：首句立即，后续最多三句。分组耗时未归档为公开指标，不用于当前默认值的性能验收；本轮也没有新的盲听质量评分。首句不等后文；后续组等到组满、字符数边界或 Agent final/end，不设置跨工具等待的额外 flush timer。Melo 始终按句提交。

Melo 固定 CPU 4 线程、原生 44.1 kHz，保留四个 FST 规则，启动 warmup 后 ready。`synthesize(..., sample_rate=...)` 支持 8/16/24/44.1/48 kHz，按 native callback 音频块做多相重采样；主应用使用 44.1 kHz 原生输出。ASR 固定 CPU 2 线程、16 kHz 单声道、`rule2_min_trailing_silence=0.8`。

`APP_ENV=test` 时未显式配置的 provider 默认为 fake。Fake TTS 输出可验证的测试音调，**不是语音**；Fake ASR 不伪造转写。

## 5. 契约、取消与指标

- 上行严格按 `web/rtc/src/protocol.js`：16B 大端头 + 640B 小端 PCM16；A1/v1/16000 Hz/320 samples。检测重复/倒序和 seq 丢帧，重连后重置序号。`speech_ended` 调 `finish()`；服务端 endpoint 也可自动产出 final。非空 final 交给编排器，在 turn 锁内分配 ID 后发送唯一一条 `transcript(final=true, turn_id=...)`，保留 confidence/audio_start_ms/audio_end_ms，与后续 Agent/TTS/turn_result 使用同一 ID。partial 尚未建立业务 turn，仅更新字幕，不猜测下一个 ID；静音/endpoint reset 后的空 final 不发送、不创建 turn。
- `AsrStream.finish()` 以 280 ms 为 deadline，排入 final 或 `ASR_FINISH_TIMEOUT`；原生计算若尚未返回，丢弃该 stream，绝不并发 reset 它。300 ms 是适配器事件期限，不含网络背压。
- 下行为 JSON `audio_chunk.pcm_b64`，带实际 sample rate、seq、reply/turn ID。每个**成功**合成组的真实 PCM 后，编排器统一发送且仅发送一条空 PCM 的 `is_last=true`；全 reply 的 seq 连续递增，终止帧不增加样本时长、不改变 t7/t8。失败/取消用 error/tts_completed(interrupted=true) 收尾，不伪造成功组终止帧，cancel 后不发旧音频。空组终止帧不会结束前端 reply，整轮仍以 `tts_completed` 为准。`tts_started.audio_format=pcm16` 强制客户端 PCM 模式，慢首包/错误不切浏览器语音。PCM16 正确归一化为 Web Audio Float32。
- `t7` 是编排器收到首个非空 TTS 块，`t8` 是首块实际发入下行连接；只记第一次。`t9` 只接收匹配 turn/reply 的 `playback_first_frame`，未收到则为 null。旧 fake HTTP 评测保留显式 `playback_measured=false` 的模拟值，不能作为真实播出指标。
- `tts_completed.total_ms` 是本轮实际输出样本数除以输出采样率，不附加虚构间隔；中断时 `interrupted=true`，时长是已输出音频，播放时长单独用客户端 `played_ms`。
- 每组首块 PCM 前（且在 `tts_started` 后）发送全组 `tts_sentence`：保留原文本、句 ID、char offsets，临时按 180 ms/字符估时，标记 `provisional=true/estimated=true/tts_alignment=false`。该组结束后用相同 sentence_id 覆盖为实际样本计数的时间轴，标记 `provisional=false`；前端已有 upsert 语义，不重复字幕或播音，游标和 UI 识别临时估算标记。单句合成最终起止按样本精确；多句组内部仍按字数比例分配且 `estimated=true/tts_alignment=false`，不声称模型给出了句级 timestamp。整组起止、整轮时长精确。可设 `QWEN_GROUP_SENTENCES=1` 获得最终单句起止，代价是韵律/吞吐权衡；生成期间的句时长仍是估算。
- 取消会关闭 Qwen socket，sidecar 监听 disconnect，停止 producer 并关闭 generator；Melo callback 返回 0 停止。native GPU kernel / ONNX 调用不可抢占，最多等当前 codec 块/callback 完成才释放模型锁。消费者立即停止 yield；发送锁内再次检查 CancelToken，cancel 帧之后不发旧 reply 音频。慢客户端使用有界队列/发送超时，模型忙时显式报错。

## 6. 测试

```powershell
.venv\Scripts\python.exe -m pytest -q
npm --prefix web/rtc test
npm --prefix web/rtc run check

# 指定本地模型才启用可选集成测试；无模型默认 skip。
$env:ASR_MODEL_DIR='D:\models\sherpa-onnx-streaming-zipformer-zh-int8-2025-06-30'
$env:MELO_MODEL_DIR='D:\models\vits-melo-tts-zh_en'
$env:VOICE_TEST_QWEN_URL='http://127.0.0.1:8090'
.venv\Scripts\python.exe -m pytest -q -m integration
```

集成测试回灌的是非实时合成语音，不能据此推断麦克风噪声环境准确率。

## 7. 上线前剩余验证

先确认当前选定参考音色的产品授权；未确认前只作本地试听。若需要更换参考，须由用户另行决定并重新试听验收，程序不自动换声音。在目标 Linux GPU 环境构建镜像，验证 CUDA/驱动、显存、容器只读模型和实际负载。当前实现每个模型只有一个生成槽位，需要容量规划和上层重试/转人工策略。验证真实浏览器播放、麦克风/AEC、网络丢包与带噪 ASR；本次自动检查不等价于这些验证。原项目鉴权/LiveKit 生产接入边界未因此消失，部署不要开启 `DEV_NO_AUTH=1` 对公网提供服务。
