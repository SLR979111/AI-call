# 预测候选音频预算实验

比较“每条预测候选都预合成音频”与“只预合成前 N 条”的离线实现。脚本不调用真实
DeepSeek/ASR/TTS 接口，只使用 fake Cloud、脚本化 Brain 和受控 `asyncio.sleep` 延迟。

实现开关：

```text
PREDICTION_AUDIO_LIMIT=
```

- 不设置或留空：保持现行为，每条候选都预合成；语音模式下没有 PCM 的候选不发布。
- `PREDICTION_AUDIO_LIMIT=N`（N 为正整数）：只给按到达顺序的前 N 条候选预合成。
  超出预算的候选仍以文本发布；被选中时走普通 TTS。
- `PREDICTION_AUDIO_LIMIT=0`：完全不预合成，全部候选先以文本发布。

## 运行

在 `new/` 目录：

```powershell
uv run python scripts/experiment_prediction_audio_budget.py
```

或使用已经装好依赖的解释器：

```powershell
python scripts/experiment_prediction_audio_budget.py
```

可选参数：

```powershell
python scripts/experiment_prediction_audio_budget.py --limits all,0,1,2,3,5,10
python scripts/experiment_prediction_audio_budget.py --output runtime/validation/prediction-audio-budget
```

默认输出到本目录：

- `results.json`：每档、每候选、每脚本化短答的完整数据。
- `results.md`：可直接阅读的对比报告，包含总览、逐候选时间、短答可用性、TTS 回退检查、结论。

## 测试

```powershell
python -m pytest -q tests/test_prediction_audio_limit.py
```

覆盖默认全量预合成、N=2 截断、预算内无音频仍不发布、N=0 全文本、文本候选复用回退 TTS。

## 读结果时注意

- 这是合成负载；延迟、并发排队和真实候选命中分布都不是生产测量。
- 生产中预测分支并发完成，`前 N 条` 指实际到达顺序。
- 音频预算只缩短预合成音频尾巴；它不会让最后一条候选文字更早到达。
- 默认不设置环境变量时，线上语义不变。
