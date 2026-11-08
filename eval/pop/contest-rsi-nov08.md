# POP 外呼联调与多场景 RSI

| 模块 | 本次更新 |
| --- | --- |
| Agent | 按已核验申请解释原因、指出缺少的细节并给整改步骤。支持口语追问及「继续」。本机 Qwen 生成可选，限时 900 ms。 |
| 会话与界面 | 通话前可选 10 个电话场景。每通固定独立的模拟申请、知识引用和写回记录。 |
| 语音 | 赛题浏览器用约 900 ms 静音收轮并保留开口前的短音频。Melo 在打断后短暂等待旧合成释放，默认语速 0.93，并规范独立缩写的读法。 |
| RSI | DeepSeek 自动出题、盲评和根据开发失败提出场景短语候选。独立留出、历史回归和安全检查通过后才更新本机策略。 |

## Windows 本地运行

依赖和模型目录见 [语音运行指南](../../docs/guides/production-voice.md)。以下命令假定模型已准备好。

```powershell
python -m pip install -r requirements.txt -r requirements-rtc.txt
$asr = 'models/sherpa-onnx-streaming-zipformer-zh-int8-2025-06-30'
$tts = 'models/vits-melo-tts-zh_en'
$run = 'data/contest-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff')
python scripts/serve_pop_contest.py --asr-model $asr --tts-model $tts --output $run --prime-tts --rtc-pcm --prepare-audio
```

打开 [通话页](http://127.0.0.1:8091/rtc/?downlink=pcm&transport=rtc_pcm)，先选「测试场景」。Windows 需要显式加载 ONNX Runtime DLL 时，在启动命令加 `--dll-dir <包含 onnxruntime.dll 的目录>`。

演示默认采用仓库中随版本发布的场景策略，后续本机通过门控的策略优先。可用 `--no-scene-policy` 重放原始基线，或用 `--scene-policy` 指定已验证的策略文件。默认关闭 Qwen 生成。若本机已运行兼容的 llama-server，可加 `--local-answer-port 18099`。

## 自动评测

配置 `POP_JUDGE_BASE_URL`、`POP_JUDGE_MODEL`、`POP_JUDGE_API_KEY` 后运行。问答和轨迹保存在本机 `data/pop-scene-rsi/runs`。通过门控后，`--publish` 更新供下次本机启动读取的配置。

```powershell
python -u scripts/run_pop_scene_rsi.py --publish
python -m pytest -q
npm --prefix web/rtc test
```

## 11 月 8 日验收记录

- DeepSeek 生成 60 条开发题和 60 条留出题。留出从 19/60 提到 39/60，主体名称从 0/6 提到 5/6。下一批 30 条留出未见增益，保留已采用版本。
- 主体名称追问「到底是执照号还是公司名不对」时，原先只复述标签或反问。新版说明应核对执照号码和公司名称，并指出无法确认具体错项。授权链路追问「少了哪一段」时，新版说明需补齐链路，具体缺失的授权双方要查本单书面意见。
- 同一条合成录音的 160、520、850 ms 句中停顿均由三轮变为一轮。含完整静音等待的浏览器首声约 1.60–1.73 秒。录音回放仍有个别转写错词，叠跑曾出现合成报忙。
- 留出文本首句最长 29.3 ms，和语音首声分别计量。
