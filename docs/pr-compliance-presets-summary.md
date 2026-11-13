# PR: 合规身份分支 / POP 预设 / 8087 云端实验验收总结

> 说明：核心代码已经合并到 `master`（本次 PR 仅补充验收、运行与回滚说明，避免再次改业务代码）。

## 1. 背景

外呼场景暴露两个问题：

1. 联系人明确说“不是本人”后，Agent 仍继续案件说明，没有安全退出。
2. 用户说“好/好的”时，阶段顺序不稳定，已经确认过的身份或原因被重复询问。

## 2. 已修复内容

### 2.1 身份合规分支

| 输入 | 行为 |
|---|---|
| 不是 / 不是本人 / 我不是店铺负责人 / 打错了 | `wrong_contact`，播放道歉，`SessionEnded` |
| 嗯 / 应该是吧 / 为什么没通过 / 你先告诉我 | `pending_identity`，只重新确认负责人，不透露案件 |
| 是的 / 对 / 没错 / 我是本人 | 固定驳回原因 + 补材料 |
| 好的谢谢 / 再见 | `closing`，播放再见，`SessionEnded` |
| 投诉 / 转人工 | 转人工 |
| 骗子 / 骚扰 / 别再打 | 安全结束 |
| 忙 / 没空 / 晚点 | 礼貌结束 |
| 已经提交 / 补过了 | 不再重复要求提交 |

关键约束：

```text
身份确认前不输出品牌、申请单号、驳回原因、补材料内容
身份确认后不再重复询问身份
每个阶段只推进一个动作
```

### 2.2 POP 阶段预设

新增 `app/agent/pop_presets.py`：

```text
REASON → REMEDIATION → CONFIRM_ACTION → WRITEBACK → CLOSE
```

身份确认时已经说过“原因 + 补材料”，FlowEngine 会直接推进到 `CONFIRM_ACTION`，下一句“好”不会再复述原因。

### 2.3 FlowPack 编码修复

`flowpacks/pop_qualification_audit.json` 之前是中文乱码，已修复为可读 UTF-8；否则阶段话术不可靠。

### 2.4 本地 8087 启动脚本

```text
scripts/start-8087.ps1
scripts/start-8087.cmd
scripts/stop-8087.ps1
scripts/stop-8087.cmd
```

只操作 8087，不影响正式 8081/8091。密钥通过本地 `.env.8087` 或 Windows 环境变量加载，不入库。

## 3. 验证证据

### 3.1 单元/分支测试

```text
106 passed, 5 skipped
```

跳过项为需要额外依赖的向量搜索测试，不是业务失败。

覆盖：

```text
身份否认
身份不明确
身份确认
结束语
阶段顺序
正常/异常预设
```

### 3.2 8087 音频级 E2E

```text
“不是” → wrong_contact → SessionEnded
“嗯” → pending_identity → 只重新确认负责人
“好的谢谢” → closing → SessionEnded
```

### 3.3 正式主干预备

```text
候选镜像：aicall-app:candidate-8833132
隔离 8088 /healthz=200
正式 8081：仍为旧镜像 aicall-app:local，未切换
8091：未启动
```

正式升版使用：

```powershell
.\scripts\promote-8081-candidate.ps1          # dry-run
.\scripts\promote-8081-candidate.ps1 -Execute # 正式窗口才执行
```

## 4. 明确未完成

```text
8081 正式容器尚未切换
8091 尚未启动
test_contract / test_voice 受本机 numpy 环境限制未跑
正式 P50/P95 尚未重新采集
```

## 5. 回滚

```text
旧镜像：aicall-app:local（构建于 2026-10-31）
候选镜像：aicall-app:candidate-8833132
脚本会先备份旧镜像，失败自动回滚
```

## 6. 风险与注意事项

```text
8087 是实验入口，不能当正式 8091
8081 当前 healthz=200 不代表新代码已上线
正式切换必须单独窗口
```
