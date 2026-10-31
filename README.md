# 智语外呼 · Voice Agent Skeleton

FastAPI skeleton for the 智语外呼 voice-agent competition entry. This is the
**fake skeleton** from the W1 integration plan: it exposes the HTTP control
plane, serves a static demo UI and records an append-only event log, using
deterministic fakes for ASR, the agent and TTS.

No model weights, no GPU and no external network calls are needed. Real CPU
ASR/TTS, the cloud LLM and WebRTC/LiveKit are later milestones; architecture,
contracts and milestone plans live in [docs/](docs/) (start with
[docs/05-week1-integration-plan.md](docs/05-week1-integration-plan.md) and
[docs/09-delivery-runtime.md](docs/09-delivery-runtime.md)).

## Requirements

| Tool | Version | Needed for |
|---|---|---|
| Python | 3.12 | local run and tests |
| Docker Engine + Compose | Compose v2.24+ | container run |
| make | any POSIX make | optional shortcuts |

## Quickstart (local)

```bash
python -m venv .venv
source .venv/bin/activate        # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
uvicorn app.main:app --reload --port 8081
```

The defaults in `app/config.py` match [.env.example](.env.example), so no
`.env` is needed locally; export a variable in your shell to override one.
Docker Compose loads `.env` automatically.

Or use the Makefile:

```bash
make install
make run
```

Then open:

- Demo UI: <http://localhost:8081/>
- Health: <http://localhost:8081/healthz>
- OpenAPI: <http://localhost:8081/docs>

### Troubleshooting: WebSocket 404

If uvicorn logs `No supported WebSocket library detected` (seen with
`websockets` 16.x), install `requirements.txt` (it pins `<16`) or start with:

```bash
uvicorn app.main:app --ws websockets-sansio --port 8081
```

### One text turn with curl

```bash
curl -s http://localhost:8081/healthz

SESSION_ID=$(curl -s -X POST http://localhost:8081/sessions \
  -H 'Content-Type: application/json' \
  -d '{"mode":"text"}' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["session_id"])')

curl -s -X POST "http://localhost:8081/sessions/$SESSION_ID/text" \
  -H 'Content-Type: application/json' \
  -d '{"text":"你好，我想查询一下我的订单。"}'

curl -sN "http://localhost:8081/sessions/$SESSION_ID/events?since_seq=0"
```

### LLM mode without a cloud key

Run the local OpenAI-compatible mock, then point the app at it:

```bash
python scripts/mock_llm.py --port 9000

AGENT_PROVIDER=llm \
LLM_PRIMARY_BASE_URL=http://127.0.0.1:9000/v1 \
LLM_PRIMARY_API_KEY=mock \
LLM_PRIMARY_MODEL=mock \
uvicorn app.main:app --port 8082
```

This exercises `LLMAgent → LLM Router → Tool Gateway → write-back` without
spending cloud quota.

## Docker

```bash
cp .env.example .env
make docker-build
make docker-up

curl -fsS http://localhost:8081/healthz
docker compose logs -f app
make docker-down
```

Equivalent raw commands:

```bash
docker build -t aicall-app:local .
docker compose up -d
```

Notes:

- The image is CPU-only and intended for `linux/amd64` (the competition
  runtime); there is no GPU profile in the fake skeleton.
- `docker-compose.yml` starts only `app`. LiveKit, CPU ASR/TTS and a GPU
  override are commented placeholders for later milestones.
- `./data` is bind-mounted to `/app/data` for SQLite, JSONL event logs and
  artifacts; it is gitignored.
- `.env` is optional with Compose v2.24+; without it the image defaults apply.
- The competition forbids external tunnels, so none are included.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Static demo UI from `web/` |
| GET | `/healthz` | Liveness plus runtime metadata |
| GET | `/metrics` | Minimal Prometheus text (`aicall_up`, `aicall_sessions`) |
| POST | `/sessions` | Create a session (`mode`: `text` or `voice`, optional `scenario_id`) |
| GET | `/sessions/{session_id}/state` | Current turn/flow state |
| POST | `/sessions/{session_id}/text` | Run one text turn |
| POST | `/sessions/{session_id}/cancel` | Cancel the current turn (debug) |
| POST | `/sessions/{session_id}/end` | End a session |
| GET | `/sessions/{session_id}/events?since_seq=0` | NDJSON event stream |
| WS | `/ws/session/{session_id}` | Skeleton media/control socket |
| POST | `/eval/sessions` | Run a headless text evaluation session |
| GET | `/eval/sessions/{eval_session_id}` | Read a stored evaluation record |
| POST | `/internal/tools/{tool}/invoke` | Invoke a tool through the gateway |
| POST | `/internal/llm/complete` | Debug: call the LLM router (503-like error if unconfigured) |
| GET | `/voice/status` | Active voice provider / readiness |
| GET | `/media/status` | LiveKit configuration status |
| POST | `/sessions/{session_id}/rtc` | Mint a LiveKit token (503 if unconfigured) |
| POST | `/knowledge/search` | Local RAG search with citations |
| GET | `/flowpacks` / `/flowpacks/{id}` | List/read Flow Packs |
| POST | `/flowpacks/{id}/simulate` | Apply intent sequence to a Flow Pack |
| POST | `/eval/run` | Run an eval suite (default `eval/cases/dev.json`) |
| GET | `/registry/production` / `/registry/releases` | Current/tracked releases |
| POST | `/registry/candidates` / `/promote` / `/rollback` | Candidate lifecycle |
| POST | `/rsi/compare` | Compare baseline vs candidate (bootstrap CI + gate) |
| POST | `/rsi/promote` | Promote only if the regression gate passes |

The source of truth for payloads is
[docs/03-interface-contract-v0.1.md](docs/03-interface-contract-v0.1.md),
[docs/11-contract-v0.2-patch.md](docs/11-contract-v0.2-patch.md) and
[docs/04-event-schema-v0.1.md](docs/04-event-schema-v0.1.md). The fake skeleton
implements the W1/W2 subset; real ASR/TTS and LiveKit servers are later
milestones.

## Make targets

| Target | Command |
|---|---|
| `make install` | `python -m pip install -r requirements.txt` |
| `make run` | uvicorn on `0.0.0.0:8081` with `--reload` |
| `make test` | `pytest -q` |
| `make contract-test` | `python contracts/validate.py contracts/fixtures` then `pytest tests/test_contract.py -q` |
| `make smoke` | `sh scripts/smoke.sh` (server must already be running) |
| `make docker-build` | `docker build -t aicall-app:local .` |
| `make docker-up` | `docker compose up -d --build` |
| `make docker-down` | `docker compose down` |

Override defaults on the command line, for example
`make run PORT=9000` or `make test PYTHON=python3.12`.

## Configuration

| Variable | Example | Meaning |
|---|---|---|
| `APP_ENV` | `dev` | Runtime profile (`dev` / `staging` / `prod`) |
| `APP_BASE` | `http://127.0.0.1:8081` | Frontend/dev base URL (see `config/app.example`) |
| `RELEASE_ID` | `rel_dev_001` | Release pinned into every session and event |
| `DATA_DIR` | `./data` | SQLite / JSONL / artifact directory |
| `DEV_NO_AUTH` | `1` | Dev-only auth bypass flag (reserved; auth is not enforced in the fake skeleton) |
| `AGENT_PROVIDER` | `fake` | `fake` or `llm` (LLM Router path; needs keys) |
| `LLM_PRIMARY_BASE_URL` | `https://api.deepseek.com/v1` | Primary OpenAI-compatible endpoint |
| `LLM_PRIMARY_API_KEY` | `sk-...` | Primary key (server-side only) |
| `LLM_PRIMARY_MODEL` | `deepseek-chat` | Primary model |
| `LLM_SECONDARY_*` | same keys | Optional secondary provider |
| `LLM_TIMEOUT_MS` | `3000` | Per-provider timeout |
| `LLM_DAILY_BUDGET_CNY` | `100` | Budget circuit breaker |
| `VOICE_PROVIDER` | `fake` | `fake` / `sherpa` / `funasr` / `cosyvoice` |
| `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | — | Enables `/sessions/{id}/rtc` token |

Without LLM keys, `/internal/llm/complete` returns `LLM_NOT_CONFIGURED` and the
fake agent continues with rule/template replies.

Copy [.env.example](.env.example) to `.env`; never commit `.env`.
The human-readable copy with `APP_BASE=http://127.0.0.1:8081` is
[`config/app.example`](config/app.example).

## Tests

```bash
make test            # full pytest suite
make contract-test   # schema validation + contract tests
make smoke           # requires `make run` or `make docker-up` in another shell
```

`make contract-test` runs exactly:

```bash
python contracts/validate.py contracts/fixtures
pytest tests/test_contract.py -q
```

`scripts/smoke.sh` checks `GET /healthz`, creates a text session, posts one text
turn and fetches the event stream. Point it at another host with
`BASE_URL=http://127.0.0.1:8081 sh scripts/smoke.sh`.

Replay one session's event timeline and generate a self-test report:

```bash
python scripts/replay.py --session s_...
python scripts/report.py --data-dir data --out reports/self-test.md
python scripts/rsi_compare.py --baseline data/baseline.json --candidate data/candidate.json
```

CI (`.github/workflows/ci.yml`) runs on `ubuntu-latest` with Python 3.12:
installs `requirements.txt`, runs `pytest -q`, then validates
`contracts/fixtures` against the JSON Schemas.

## Project structure

```text
aicall/
├── app/                  FastAPI app (orchestrator, agent, llm, rag, flow, rsi)
├── web/                  Static demo UI served at /
├── contracts/            JSON Schemas, fixtures and validate.py
├── knowledge/            Local FAQ knowledge base
├── flowpacks/            Business flow definitions
├── eval/                 Evaluation case suites
├── reports/              Generated self-test reports
├── tests/                pytest suite (including test_contract.py)
├── scripts/              smoke.sh, replay.py, report.py, rsi_compare.py
├── docs/                 Architecture, contract and delivery documents
├── data/                 Runtime SQLite/JSONL/artifacts (created, gitignored)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── Makefile
├── .env.example
└── .github/workflows/ci.yml
```

## Scope of this skeleton

- Deterministic fakes for ASR, agent and TTS; no model files are required.
- In-memory/JSONL/SQLite event trail with a monotonic `seq`, suitable for the
  replay and latency work described in [docs/04](docs/04-event-schema-v0.1.md).
- Out of scope for now: real ASR/TTS, cloud LLM integration, LiveKit/WebRTC,
  coturn, authentication and multi-scenario Flow Packs.
