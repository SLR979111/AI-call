# Developer entrypoints for the fake skeleton. See README.md for details.
# Override the interpreter if `python` is not Python 3.12:
#   make test PYTHON=python3.12

PYTHON ?= python
APP    ?= app.main:app
HOST   ?= 0.0.0.0
PORT   ?= 8081
IMAGE  ?= aicall-app:local

.PHONY: help install run test contract-test smoke report mock-llm docker-build docker-up docker-down

help:
	@echo "make install       Install runtime + test dependencies"
	@echo "make run           Run the API with autoreload on port $(PORT)"
	@echo "make test          Run the full pytest suite"
	@echo "make contract-test Validate fixtures and run contract tests"
	@echo "make smoke         Health check + one text turn (server must be running)"
	@echo "make report        Generate reports/self-test.md from data/sessions"
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

report:
	$(PYTHON) scripts/report.py --data-dir data --out reports/self-test.md

mock-llm:
	$(PYTHON) scripts/mock_llm.py --port 9000

docker-build:
	docker build -t $(IMAGE) .

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down
