# Developer entrypoints for the fake skeleton. See README.md for details.
# Override the interpreter if `python` is not Python 3.12:
#   make test PYTHON=python3.12

PYTHON ?= python
APP    ?= app.main:app
HOST   ?= 0.0.0.0
PORT   ?= 8081
IMAGE  ?= aicall-app:local
COMPOSE ?= docker-compose
METRIC_BASE ?= http://127.0.0.1:8081
METRIC_SECTION ?= all

.PHONY: help install run test contract-test smoke e2e report metric metric-closure metric-latency metric-task metric-compliance metric-barge mock-llm docker-build docker-up docker-down

help:
	@echo "make install       Install runtime + test dependencies"
	@echo "make run           Run the API with autoreload on port $(PORT)"
	@echo "make test          Run the full pytest suite"
	@echo "make contract-test Validate fixtures and run contract tests"
	@echo "make smoke         Health check + one text turn (server must be running)"
	@echo "make e2e           2 turns + barge-in + hangup over WS"
	@echo "make report        Generate reports/self-test.md from data/sessions"
	@echo "make metric        Run all competition metric checks"
	@echo "make metric-closure / metric-latency / metric-task / metric-compliance / metric-barge"
	@echo "                   Run one metric section (server must be running)"
	@echo "make mock-llm      Start a local OpenAI-compatible mock on :9000"
	@echo "make docker-build  Build the image $(IMAGE)"
	@echo "make docker-up     Build and start the compose stack in the background"
	@echo "make docker-down   Stop and remove the compose stack"

install:
	$(PYTHON) -m pip install -r requirements.txt

run:
	$(PYTHON) -m uvicorn $(APP) --host $(HOST) --port $(PORT) --reload

test:
	$(PYTHON) -m pytest -q

contract-test:
	$(PYTHON) contracts/validate.py contracts/fixtures
	$(PYTHON) -m pytest tests/test_contract.py -q

smoke:
	sh scripts/smoke.sh

e2e:
	$(PYTHON) scripts/e2e_ws.py --base http://127.0.0.1:8081

report:
	$(PYTHON) scripts/report.py --data-dir data --out reports/self-test.md

metric:
	$(PYTHON) scripts/metric_check.py --base $(METRIC_BASE) --section $(METRIC_SECTION)

metric-closure:
	$(PYTHON) scripts/metric_check.py --base $(METRIC_BASE) --section closure

metric-latency:
	$(PYTHON) scripts/metric_check.py --base $(METRIC_BASE) --section latency

metric-task:
	$(PYTHON) scripts/metric_check.py --base $(METRIC_BASE) --section task

metric-compliance:
	$(PYTHON) scripts/metric_check.py --base $(METRIC_BASE) --section compliance

metric-barge:
	$(PYTHON) scripts/metric_check.py --base $(METRIC_BASE) --section barge

mock-llm:
	$(PYTHON) scripts/mock_llm.py --port 9000

docker-build:
	docker build -t $(IMAGE) .

docker-up:
	$(COMPOSE) up -d --build

docker-down:
	$(COMPOSE) down
