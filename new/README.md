# AI Call G2

这是仓库 `new/` 目录下独立运行的最小实现，使用自己的依赖、任务卡和 `.env` 配置。首次安装示例从仓库根目录开始，其他命令均在 `new/` 内执行。

一个从零编写的、有目标的语音对话最小实现。给它任务目标和已知信息，它负责听取、解释、追问、确认和结束。可随时打断，也可用文字试聊。

默认工单围绕京东商家的品牌资料补正展开。商家可以追问缺哪段授权、公司名称为何对不上、商标截图要显示哪些信息，也可以在通话中说明已补交，改问审核进度。默认工单位于 `tasks/jd-qualification.json`，编辑过的自定义任务保存在当前浏览器。

现场回复和预测回复都使用相同的口语要求：直接答问题、说明原因或问缺少的信息，不播报“先记录/下一步判断”等工作过程。模型接口只读取正文，不读取独立思考字段；明显的思考标签、分析标题和内部旁白会在进入真实历史、页面及语音合成之前被拒绝。这个检查用于拦截明确标记，不是对任意措辞的语义保证。

只复用了本机已有的 DeepSeek API 凭据及接口、模型、音色配置。没有导入旧 AI-call 的代码、前端、模型权重、运行环境或业务流程。运行时只依赖本目录和配置的推理服务。

## 使用

双击 `start.cmd`，浏览器打开 <http://127.0.0.1:8088>。停止服务用 `stop.cmd`。

1. 打开底部的“任务设置”，在“互动工单”中选择场景，点击“保存任务”应用选择。可编辑工单目标、已知信息、要点和开场白。旧版界面仍可通过 `/static/classic.html` 访问。
2. 点击“开始通话”使用麦克风，或点击“文字试聊”。语音模式也可以输入文字。扬声器播放时，收音端使用实际播放的音频做消回声。
3. 说话或点“打断”会停止当前回复。下一轮按浏览器已播放的采样数估算句中的文字位置，完整播放的要点才会打勾。要点说完、对方确认理解且没有新问题时，客服可以先道别，等最后一句播完再挂断。

工单库内置 11 张京东商家资质工单，覆盖品牌资料补正、补交进度、授权主体、主体资质到期、商标截图、企业更名、质检报告型号、许可附页、授权商品范围、商标续展和双品牌附件错放。每张工单有自己的记录、处理条件和完成要点。选择后可以直接用语音或文字追问、改口、确认或结束。通过 `/?pack=jd-brand-mixup` 可以直接接听双品牌工单，回放库中的六个例子也各有“进入互动通话”入口。工单数据为虚构案例；对话不会改动京东后台。

页面上的接话耗时从用户最后一帧语音算到正文开始播放。要复测同一段输入，在已启动服务时运行 `node scripts/check-first-response.mjs <24kHz单声道PCM16文件> current`。每轮分别报告最终识别、正文文字到达和正文音频开始的时间。可用 `CALL_URL` 指定服务。运行 `node scripts/check-interrupted-call.mjs` 可在冻结文字动画时验证真实播放、打断续谈和主动道别。

新机器需要 Python 3.12 和 uv，然后：

```powershell
cd new
uv sync --group dev
Copy-Item .env.example .env
# 在 .env 填入 DEEPSEEK_API_KEY
.\scripts\start.ps1
```

服务只监听本机。配置保存在 `.env`，不会通过网页接口返回；`.env` 和 `runtime/` 均被 Git 忽略。任务编辑保存在当前浏览器的 localStorage，下次打开仍可使用。

## 最小运行方式

```text
持续收音 → 短停顿预识别 → 确认话轮 → 一个对话模型请求
                                      ├─ 复用适用且已就绪的预测音频
                                      └─ 直接生成短句，流式合成、播放
麦克风 + 实际播放参考 → 消回声/降噪 → 本地人声判断
确认用户插话 → 后端取消 + 客户端停播 → 旧版本结果全部失效
播放回执 → 已说明要点；用户明确确认 → 对话目标完成
```

- **任务状态**只保存用户事实、待解决问题、已完整播出的要点、最后问句和结果。没有固定业务阶段。
- **实时回应**使用一条流式模型请求。模型先输出一两条可说的话，最后提交有原文依据的状态更新。问题元数据必须确实出现在发言文本里。
- **预测**在本轮播报时按五类可能的回答并行生成候选（`PREDICTION_BRANCHES` 可调 1-5），每类最多两条。候选文字一到就先发布文本，音频在后台准备；上一轮完整播完后才可复用。命中时如果音频已就绪就用预测音频，没就绪就回退普通语音合成，不再为了等音频而扣住文字候选。选择时参考上一句问话和完整的用户原话，含糊或多重诉求交给模型判断。实验开关 `PREDICTION_AUDIO_LIMIT` 只限制前 N 条候选的音频预合成（不设或留空维持原行为），不影响文本发布；被选中但未预合成的候选走普通 TTS，用于比较音频预算与命中率。
- **命中后不阻塞**：预合成音频先播；缓存回复的状态 `update` 在后台请求，播放完成和 `reply_done` 不必等它。更新返回后再补写状态，用户打断则取消旧更新。
- 开场相同、整张任务卡和模型完全一致时，可复用上次生成的开场候选。开场预测、LLM/ASR 结果和 TTS PCM 都写入本地 SQLite（`CACHE_PATH`），进程重启或新对话仍可直接读取；开场预测最多保留 16 张任务卡，后续轮次仍依据本次沟通重新准备。
- **声音**始终上行。浏览器使用 AudioWorklet 采集并重采样为 16 kHz；下行是 24 kHz PCM。实际送往扬声器的声音同时送入 AEC3 参考通道，经过消回声与降噪后用本机 Silero VAD 复核。播放期间还检查残余声音与播放参考的相似度；启动阶段用多路延迟比对保护真正插话。自动打断只由后端这一处触发，前端不再凭音量抢先停播。手动打断仍立即生效。
- **本地识别加速**：终稿识别优先使用本机 SenseVoice（`LOCAL_ASR=1`，默认开启），短句约 0.1–0.2 s，命中预测时首句音频约 0.3–0.4 s；模型缺失或识别为空时自动回退云端 `/v1/tools/asr`。
- **插话证据**：播放期间要求最近320 ms中至少160 ms的人声证据，允许音节间的小间隙；保留600 ms前置音频，避免截掉首字。底噪只在未播放且人声概率低时学习，回声不会把人声门槛越抬越高。每次回复有独立版本，客户端和服务端都丢弃取消后的旧结果。
- **播放证据**按句子记录；约 500 ms 的下行播放窗口防止大量音频堆积。播放回执只证明客户端播放进度，不证明用户听见或理解。
- **整段 ASR 的延迟处理**：短暂静音约 160 ms 时启动识别，约 480 ms 静音后提交（确认/否定类短答 260 ms，问句 340 ms）。识别尚未完成时若用户续说，保留整段音频并撤销旧结果；长句也会做粗略预识别。少量明显未完结的连接词延长等待到约 1150 ms。这不是原生流式 ASR，也不是语义判停模型。

## 文件

| 文件 | 职责 |
|---|---|
| `app/models.py` | 任务卡、会话状态、完成条件 |
| `app/brain.py` | 当前回应、可丢弃预测、流式输出解析 |
| `app/session.py` | 取消、音频队列、播放回执、会话日志 |
| `app/hearing.py` | 收音、判停、预识别和续说合并 |
| `app/vad.py`、`app/assets/` | 本机人声检测与带许可证的公开模型 |
| `app/providers.py` | 新写的 ASR/LLM/TTS HTTP 适配、音频缓存 |
| `app/persistent_cache.py` | 跨对话/跨重启的 SQLite 缓存 |
| `app/main.py` | 一个服务和一个 WebSocket 入口 |
| `web/` | 无构建步骤的页面、采集和播放器 |
| `web/acoustics.js`、`web/vendor/` | 播放参考比对、AEC3、降噪及其许可证 |

互动工单的场景事实由任务卡提供，实际业务查询、登记、派单和转接仍需在外部系统办理。预测、LLM、ASR 和 TTS 缓存保存在 `runtime/cache.sqlite3`（可用 `CACHE_PATH` 改位置），日志写入 `runtime/sessions/`。

如需在缓存丢失后重建，可设置 `CACHE_AUDIT_PATH=./runtime/cache-audit.jsonl`，在 provider 边界记录完整模型请求/回复和原始 PCM；再用 `scripts/backfill_cache.py` 回灌。审计日志含对话内容，不要提交到仓库。详见 `docs/31-cache-audit-backfill.md`。

## 验证

不调用云接口的回归：

```powershell
uv run pytest -q
npm install
npm test
```

抗干扰回灌（21组回声、底噪、敲击、轻声和同时说话；两组调用真实ASR）：

```powershell
uv run python scripts/check_acoustics.py
```

浏览器声学整链路检查可单独启动 `uv run uvicorn scripts.serve_acoustic_check:app --host 127.0.0.1 --port 8091`，打开 `http://127.0.0.1:8091/acoustic-check` 后点击运行。它使用合成输入，不采集真实麦克风，也不播放到真实扬声器。测试结果及合成音频帧保存在 `runtime/validation/acoustics/`；正常应用不会保存麦克风音频。

真实服务与浏览器回灌（会调用已配置的推理 API；不录制用户麦克风）：

```powershell
uv run python scripts/check_live.py
uv run python scripts/check_jd.py
.\scripts\start.ps1
npm run test:browser
```

运行 `node scripts/check-forecast-hits.mjs current` 可测六种固定输入。加 `--short-openings` 可测另外四种短开场。脚本在开场播完后立即提交文字，记录当时已就绪的候选数、真实复用结果，以及从发送到收到正文首播回执的时间。使用合成静音麦克风、真实模型及语音播放。

浏览器检查使用本机 Edge 和 Playwright，合成测试语音经过真实 AudioWorklet、PCM 上行、ASR、对话模型和 TTS。检查桌面/移动布局、暂停、实际播放中的打断、续说及预测/直接回应路径。截图与可读事件保存在 `runtime/validation/`。实际测试结果见 [验证记录](VALIDATION.md)。

### 切换云端或纯本地通话

打开页面底部的「任务设置」，在「通话模式」选择云端或纯本地。选择立即保存到当前浏览器，从下一通电话生效。云端继续使用配置的音色。纯本地使用 Paraformer 识别语音、本机 Ollama 的 Qwen2.5 7B Instruct 生成回复、MeloTTS 的普通话女声朗读。两种模式各自保存预测与音频缓存。

首次使用纯本地或从旧版华妍音色升级时，先启动已安装的 Ollama，在项目目录运行下面的安装脚本，然后重启通话服务。安装时联网下载模型，本地通话只连接本机的 11434 端口，可以留空云端 API 密钥。缺少本地模型时，页面会显示安装提示。[Ollama](https://ollama.com/download) · [对话模型](https://ollama.com/library/qwen2.5:7b-instruct) · [MeloTTS 中文音色](https://huggingface.co/myshell-ai/MeloTTS-Chinese)

```powershell
.\scripts\setup-local.ps1
```

### 正文首字延迟

Windows 启动脚本会一次性下载校验过的 Paraformer 小模型，供本机在短暂停顿时提前准备回复。手动启动 Uvicorn 时可先运行 `uv run python -m app.local_asr`。下载失败时继续使用云识别。最终转写和对话状态均匹配后，才允许发送准备好的回复与音频。云识别在停声约 1.05 秒后仍未返回时，可采用同一段完整录音的本机识别结果。已经选定的云端正文若再等 650 毫秒仍无音频，可向同一音色发起一次补充合成，只播放先到的一路。[模型说明](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/offline-paraformer/paraformer-models.html)

在运行中的服务上执行以下命令。首次生成八条合成麦克风录音，之后可用同一批文件比较版本。每条录音各开一通电话，从输入末尾的有声窗口量到浏览器开始播放正文的首个有声窗口。接话音与音频开头的静音均排除。脚本保存逐条记录，P50 达到 2 秒或任一轮超过 3 秒时以失败退出。

```powershell
uv run python scripts/make-latency-fixtures.py
node scripts/check-first-response.mjs runtime/validation/latency-inputs current
uv run python scripts/make-latency-fixtures.py --dialogues
node scripts/check-first-response.mjs runtime/validation/latency-dialogues followups
$env:CALL_MODE = 'local'
node scripts/check-first-response.mjs runtime/validation/latency-dialogues offline
```

## 当前边界

- 当前为本地、短会话原型。抗回声与常见噪声已经过合成混音和浏览器整链路回灌；真实房间、扬声器非线性失真、咳嗽、旁人说话及附和仍需实测，不能保证所有环境下零误打断。
- 预测会减少常见回答的等待；未命中时仍需等待模型及 TTS。少量样本不能代表稳定 P50/P95。
- 用户转写和模型语义判断仍可能出错，引用检查只能限制无原话依据的更新，不能保证所有语义判断正确。
- 使用的是当前凭据可调用的 DeepSeek `/chat/completions`、`/v1/tools/asr`、`/v1/tools/tts`；后两者已做真实请求验证，未来接口变化需调整适配器。

实现 API 参考：[AudioWorklet](https://developer.mozilla.org/en-US/docs/Web/API/AudioWorklet)、[FastAPI WebSocket](https://fastapi.tiangolo.com/advanced/websockets/)。

随代码提供本地 Silero VAD 模型、WebRTC AEC3 WASM 和各自许可证；其余依赖分别由 `uv.lock`、`package-lock.json` 固定。仓库不包含 API 凭据、运行环境、测试音频和通话日志。云接口及声学回灌需自行配置 `.env`；只运行 `uv run pytest -q` 和 `npm test` 不需要凭据。
