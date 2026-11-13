# 24 · 正式主干回归清单（8081 / 8091）

> 范围：只覆盖正式主干。8087 是云端实验入口，不纳入本清单。
> 目的：在重启 8081 或启用 8091 前，有可执行、可回滚、可留证的步骤。

## 1. 当前状态（2026-11-13）

| 入口 | 状态 | 说明 |
|---|---|---|
| 8081 | 运行中，`/healthz=200` | Docker 容器 `aicall-app-1`，镜像 `aicall-app:local`（13 天前构建），未加载当前 master |
| 8091 | 未监听 | 比赛入口，尚未启动 |
| 8087 | 实验入口，`/healthz=200` | 不纳入正式主干 |

已完成的只读证据：

```text
8081 UTF-8 文本 smoke：
POST /sessions {mode:text, scenario_id:pop_qualification_audit}
POST /sessions/{id}/text {"text":"你好"}
Transcript: 你好
result: completed
```

注意：PowerShell 直接发中文 JSON 可能被编码成 `??`；正式 smoke 必须用 UTF-8 客户端（Python requests 或文件体）。

## 2. 重启 8081 前必须确认

```text
[ ] 当前工作区干净，git status 无未提交业务代码
[ ] 当前 master 已包含本轮身份/合规提交
[ ] 已记录 8081 容器名/镜像 ID/端口映射
[ ] 已保存当前 `docker compose config`（不含密钥入库）
[ ] 已确认 8087 不占用 8081 端口
[ ] 已准备旧镜像 ID 作为回滚点
[ ] 已记录当前 master commit
[ ] 已通知使用 8081 的人，避免通话中断
```

## 3. 8081 回归步骤

### 3.1 启动前

```powershell
# 8081 是 Docker 容器，不是本地 python；不要 Stop-Process
docker ps --filter name=aicall-app-1 --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}\t{{.Status}}'
docker inspect aicall-app-1 --format 'image={{.Image}} health={{.State.Health.Status}}'
docker compose config *> H:\aicall\data\formal-compose-snapshot.txt

# 记录当前提交
git rev-parse HEAD
git status --short
```

### 3.2 重启

只停 8081，不动 8087/8091：

```powershell
# 先备份旧镜像，构建新镜像，再重建 app 容器
docker tag aicall-app:local aicall-app:rollback-$(Get-Date -Format yyyyMMdd-HHmm)
docker compose build app
docker compose up -d app
```

### 3.3 启动后检查

```powershell
curl.exe -sS -o NUL -w "8081=%{http_code}`n" http://127.0.0.1:8081/healthz
curl.exe -sS -o NUL -w "8087=%{http_code}`n" http://127.0.0.1:8087/healthz
```

期望：

```text
8081=200
8087=200（不影响实验入口）
```

### 3.4 文本 smoke

用 Python，避免 PowerShell 中文编码问题：

```python
import requests
base = "http://127.0.0.1:8081"
s = requests.post(base + "/sessions", json={
    "mode": "text", "scenario_id": "pop_qualification_audit"
}, timeout=15).json()
r = requests.post(f"{base}/sessions/{s['session_id']}/text", json={"text": "你好"}, timeout=30).json()
print(r)
```

通过标准：

```text
SessionStarted / TurnStarted / AgentSentence 正常
Transcript 为“你好”，不是 ?? 或乱码
result.outcome 非 error
```

### 3.5 语音 smoke

浏览器打开：

```text
http://127.0.0.1:8081/rtc/
```

走：

```text
你好 → 是的 → 为什么被驳回 → 需要补什么 → 好的谢谢
```

通过标准：

```text
有声音
身份确认前不出现品牌/申请单/驳回原因
说“不是/不是本人” → 安全退出，SessionEnded
说“嗯” → 只重新确认负责人
说“好的谢谢” → 再见并 SessionEnded
```

### 3.6 指标

```text
TurnLatency 有 t0..t9
t9 不为 null
playback_measured=true
```

## 4. 8091 启用前检查

```text
[ ] 8081 回归通过
[ ] 8091 使用独立 DATA_DIR
[ ] 8091 与 8081 的 Key/端口/配置不串
[ ] 8091 启动命令已记录
[ ] 8091 六阶段 smoke 通过
```

8091 启动前不要复用 8087 的 `.env.8087`。

## 5. 回滚

触发条件：

```text
8081 healthz 非 200
文本 smoke 乱码或 error
语音无声音
身份确认前泄露案件
否认后未结束会话
```

回滚动作：

```text
1. 停止新 app 容器
2. 用旧镜像 ID 重新打 tag 为 aicall-app:local
3. docker compose up -d app
4. curl /healthz
5. 用 Python 文本 smoke 验证
6. 记录回滚镜像 ID/提交号和原因
```

## 6. 本轮已知限制

```text
8081 当前是 Docker 容器 `aicall-app-1`，镜像 13 天前构建，未加载当前 master
8091 未启动
test_contract.py / test_voice.py 受本机 numpy 环境限制未跑
8087 的 99 个非语音测试通过，不代表 8081 音频已验证
```

## 7. 验收记录模板

```text
日期：
执行人：
8081 旧镜像 ID：
8081 新镜像 ID：
8081 容器名：
master commit：
8081 healthz：
8087 healthz：
文本 smoke：通过/失败
语音 smoke：通过/失败
越界检查：通过/失败
否认挂机：通过/失败
结束挂机：通过/失败
回滚：未触发/已执行
备注：
```
