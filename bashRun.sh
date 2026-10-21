#!/bin/bash
# 一键运行：构建前端 + 启动后端（0.0.0.0:8000）
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$ROOT"

# 前端产物构建（没有 npm 时沿用已有的 backend/webui 产物）
if command -v npm >/dev/null 2>&1; then
    npm --prefix frontend install
    npm --prefix frontend run build
elif [ -f backend/webui/index.html ]; then
    echo "[提示] 未找到 npm，跳过前端构建，沿用已有产物 backend/webui/"
else
    echo "[错误] 未找到 npm，且 backend/webui 无产物。请先安装 Node.js 18+，或执行 ./install.sh --all" >&2
    exit 1
fi

# 后端启动服务
if [ -x "$ROOT/.venv/bin/python" ]; then
    PY="$ROOT/.venv/bin/python"
else
    PY=python3
fi
exec "$PY" backend/api.py --host 0.0.0.0 --port 8000
