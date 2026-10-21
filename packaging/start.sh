#!/usr/bin/env bash
# 启动打包好的服务：默认监听 0.0.0.0:8000，可用 HOST / PORT 环境变量覆盖
#   HOST=127.0.0.1 PORT=8080 ./start.sh
set -euo pipefail
cd "$(dirname "$0")"
exec ./llm-server-eval --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}" "$@"
