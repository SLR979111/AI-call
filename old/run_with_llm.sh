#!/bin/bash
# 正确的 LLM Agent 启动脚本

cd "$(dirname "$0")"

# 激活虚拟环境
source .venv/bin/activate

# 在虚拟环境激活后设置环境变量
export $(grep -v '^#' .env | xargs)

# 验证配置
echo "=== Environment Configuration ==="
echo "AGENT_PROVIDER: $AGENT_PROVIDER"
echo "LLM_PRIMARY_MODEL: $LLM_PRIMARY_MODEL"
echo "LLM_PRIMARY_BASE_URL: $LLM_PRIMARY_BASE_URL"
echo ""

# 启动服务
echo "Starting AI-call with LLM Agent..."
uvicorn app.main:app --host 0.0.0.0 --port 8081 --reload
