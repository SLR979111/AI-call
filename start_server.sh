#!/bin/bash
# AI-call Server Startup Script with .env loading

set -e

cd "$(dirname "$0")"

# Check .env file exists
if [ ! -f .env ]; then
    echo "ERROR: .env file not found. Please copy .env.example to .env and configure it."
    exit 1
fi

# Activate virtual environment
if [ ! -d .venv ]; then
    echo "ERROR: Virtual environment not found. Please run: python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi

source .venv/bin/activate

# Load environment variables from .env
export $(grep -v '^#' .env | xargs)

# Verify critical environment variables
if [ -z "$AGENT_PROVIDER" ]; then
    echo "WARNING: AGENT_PROVIDER not set, defaulting to 'fake'"
    export AGENT_PROVIDER=fake
fi

if [ "$AGENT_PROVIDER" = "llm" ]; then
    if [ -z "$LLM_PRIMARY_BASE_URL" ] || [ -z "$LLM_PRIMARY_API_KEY" ] || [ -z "$LLM_PRIMARY_MODEL" ]; then
        echo "ERROR: AGENT_PROVIDER=llm but LLM_PRIMARY_* not configured in .env"
        exit 1
    fi
    echo "✓ LLM Agent enabled: $LLM_PRIMARY_MODEL"
else
    echo "✓ Fake Agent enabled (rule-based)"
fi

# Start server
echo "Starting AI-call server on http://0.0.0.0:8081"
echo "Press Ctrl+C to stop"
echo ""

exec uvicorn app.main:app --host 0.0.0.0 --port 8081 --reload
