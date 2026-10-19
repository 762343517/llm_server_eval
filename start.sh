#!/usr/bin/env bash
# zhipuEval 服务启动脚本
#
# 用法：
#   ./start.sh                     # 前台启动，默认 0.0.0.0:8000
#   PORT=8010 ./start.sh           # 换端口（也可 --port 8010）
#   ./start.sh --daemon            # 后台启动，日志 runs/server.log
#   ./start.sh --stop              # 停止后台服务
#   ./start.sh --status            # 查看运行状态
#   ./start.sh --check             # 只做启动前检查，不启动
#   ./start.sh --reload            # 开发模式自动重载（前台）
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$ROOT"

# pier 启动时会去 GitHub 拉模型价目表，内网下每次 `pier --version` 要 20s+；
# 只用随包分发的本地价目表（与 backend/agent_core.py 的 setdefault 一致）
export LITELLM_LOCAL_MODEL_COST_MAP="${LITELLM_LOCAL_MODEL_COST_MAP:-true}"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
ACTION="start"          # start | stop | status | check
MODE="foreground"       # foreground | daemon
PID_FILE="$ROOT/runs/server.pid"
PORT_FILE="$ROOT/runs/server.port"
LOG_FILE="$ROOT/runs/server.log"
PORT_GIVEN=0
PASSTHROUGH=()

info() { printf '\033[1;34m[启动]\033[0m %s\n' "$*"; }
ok()   { printf '  \033[0;32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[0;33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[0;31m[错误]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    # 打印脚本头部注释（首个非注释行为止）
    awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}"
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --host)    HOST="${2:?--host 需要地址}"; shift ;;
        --port)    PORT="${2:?--port 需要端口}"; PORT_GIVEN=1; shift ;;
        --daemon)  MODE="daemon" ;;
        --stop)    ACTION="stop" ;;
        --status)  ACTION="status" ;;
        --check)   ACTION="check" ;;
        --reload)  PASSTHROUGH+=("--reload") ;;
        -h|--help) usage ;;
        *)         PASSTHROUGH+=("$1") ;;
    esac
    shift
done

# stop / status 没显式给端口时，沿用后台启动时记录的端口
if [ "$PORT_GIVEN" = 0 ] && { [ "$ACTION" = "stop" ] || [ "$ACTION" = "status" ]; } \
        && [ -f "$PORT_FILE" ]; then
    PORT=$(cat "$PORT_FILE")
fi

case "$PORT" in
    ''|*[!0-9]*) die "端口必须是数字：$PORT" ;;
esac
[ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "端口超出范围：$PORT"

# ------------------------------------------------------------------ 解释器
if [ -x "$ROOT/.venv/bin/python" ]; then
    PY="$ROOT/.venv/bin/python"
else
    PY=$(command -v python3) || die "找不到 python3，请先运行 ./install.sh"
fi
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "Python 版本过低（$("$PY" -V 2>&1)），需要 3.10+"

# ------------------------------------------------------------------ 端口
listener_line() {
    if command -v ss >/dev/null 2>&1; then
        ss -ltnp 2>/dev/null | awk -v p=":$PORT" 'NR > 1 && $4 ~ (p "$")' | head -1
    elif command -v netstat >/dev/null 2>&1; then
        netstat -ltnp 2>/dev/null | awk -v p=":$PORT" '$4 ~ (p "$")' | head -1
    fi
}

port_check() {
    local line pid
    line=$(listener_line)
    if [ -n "$line" ]; then
        pid=$(printf '%s' "$line" | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2 || true)
        warn "端口 $PORT 已被占用："
        printf '      %s\n' "$line"
        if [ -n "$pid" ]; then
            printf '      进程：%s\n' "$(ps -o args= -p "$pid" 2>/dev/null | cut -c1-120)"
            printf '      处理：kill %s   或换端口：PORT=8010 ./start.sh\n' "$pid"
        fi
        return 1
    fi
    return 0
}

probe_url() {
    case "$HOST" in
        0.0.0.0|::|"") printf 'http://127.0.0.1:%s' "$PORT" ;;
        *)             printf 'http://%s:%s' "$HOST" "$PORT" ;;
    esac
}

http_ok() {
    local url
    url="$(probe_url)/api/meta"
    if command -v curl >/dev/null 2>&1; then
        curl -sf -o /dev/null --max-time 2 "$url"
    else
        "$PY" - "$url" <<'PY'
import sys
import urllib.request
try:
    urllib.request.urlopen(sys.argv[1], timeout=2).read(1)
except Exception:
    sys.exit(1)
PY
    fi
}

# ------------------------------------------------------------------ 环境检查
check_env() {
    local fail=0

    "$PY" - <<'PY' || fail=1
import importlib.util
import sys

missing = [m for m in ("fastapi", "uvicorn", "pandas", "pyarrow", "tiktoken", "yaml")
           if importlib.util.find_spec(m) is None]
if missing:
    print("  \033[0;31m✗\033[0m 缺少 Python 依赖: " + ", ".join(missing) + "（运行 ./install.sh）")
    sys.exit(1)
print("  \033[0;32m✓\033[0m 后端依赖齐全")
PY

    if [ -f "$ROOT/backend/webui/index.html" ]; then
        ok "前端产物 backend/webui/"
    else
        warn "缺少 backend/webui/index.html（运行 ./install.sh 或 npm --prefix frontend run build）"
        fail=1
    fi

    command -v pier >/dev/null 2>&1 && ok "pier $(pier --version 2>/dev/null | head -1)（Agent 评测可用）" \
        || warn "pier 未安装：Agent 评测不可用（./install.sh --agent）"

    if docker info >/dev/null 2>&1; then
        ok "docker $(docker info --format '{{.ServerVersion}}' 2>/dev/null)"
    elif sg docker -c 'docker info' >/dev/null 2>&1; then
        ok "docker 可用（自动使用 sg docker）"
    else
        warn "docker 不可用：Agent 评测不可用"
    fi

    # 与后端查找顺序一致：EVALSCOPE_BIN → PATH（evalscope 常用 uv tool 装到 ~/.local/bin）
    if [ -n "${EVALSCOPE_BIN:-}" ] && [ -x "${EVALSCOPE_BIN}" ]; then
        ok "evalscope（EVALSCOPE_BIN=${EVALSCOPE_BIN}）"
    elif evalscope_bin=$(command -v evalscope 2>/dev/null); then
        ok "evalscope（${evalscope_bin}）"
    else
        warn "evalscope 未找到：压测不可用（./install.sh --perf）"
    fi

    port_check || fail=1
    return "$fail"
}

# ------------------------------------------------------------------ 子命令
stop_daemon() {
    [ -f "$PID_FILE" ] || die "没有 PID 文件，服务未在后台运行：$PID_FILE"
    local pid cmd
    pid=$(cat "$PID_FILE")
    if ! kill -0 "$pid" 2>/dev/null; then
        warn "进程 $pid 已不存在，清理 PID 文件"
        rm -f "$PID_FILE" "$PORT_FILE"
        return 0
    fi
    cmd=$(ps -o args= -p "$pid" 2>/dev/null || true)
    case "$cmd" in
        *backend/api.py*) ;;
        *) die "PID $pid 不是本服务（$cmd），拒绝处理" ;;
    esac
    kill "$pid"
    for _ in $(seq 1 30); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.5
    done
    if kill -0 "$pid" 2>/dev/null; then
        warn "15s 未退出，发送 SIGKILL"
        kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE" "$PORT_FILE"
    ok "已停止（PID $pid）"
}

status() {
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        local pid
        pid=$(cat "$PID_FILE")
        ok "运行中：PID $pid，$(probe_url)"
        ps -o pid=,etime=,args= -p "$pid" 2>/dev/null | cut -c1-120
        http_ok && ok "接口可访问" || warn "接口无响应"
        return 0
    fi
    warn "未在后台运行（无 $PID_FILE 或进程已退出）"
    if listener_line >/dev/null; then
        warn "但端口 $PORT 被其他进程占用：$(listener_line)"
    fi
    return 1
}

# ------------------------------------------------------------------ 启动
do_start() {
    mkdir -p "$ROOT/runs"
    check_env || die "启动前检查未通过，请先修复上面标 ✗ 的项"

    local args=(--host "$HOST" --port "$PORT")
    if [ "${#PASSTHROUGH[@]}" -gt 0 ]; then
        args+=("${PASSTHROUGH[@]}")
    fi

    info "解释器：$PY"
    info "监听：http://$HOST:$PORT（本机访问 $(probe_url)）"

    if [ "$MODE" = "foreground" ]; then
        exec "$PY" backend/api.py "${args[@]}"
    fi

    info "后台启动，日志：$LOG_FILE"
    nohup "$PY" backend/api.py "${args[@]}" >>"$LOG_FILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$PID_FILE"
    echo "$PORT" > "$PORT_FILE"

    for _ in $(seq 1 60); do
        if http_ok; then
            ok "启动完成：PID $pid，$(probe_url)"
            ok "日志：$LOG_FILE    停止：./start.sh --stop"
            return 0
        fi
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.5
    done

    warn "30s 内未就绪，最近日志："
    tail -n 20 "$LOG_FILE" 2>/dev/null | sed 's/^/      /'
    kill "$pid" 2>/dev/null || true
    rm -f "$PID_FILE" "$PORT_FILE"
    die "启动失败"
}

case "$ACTION" in
    stop)   stop_daemon ;;
    status) status ;;
    check)
        info "启动前检查（HOST=$HOST PORT=$PORT）"
        check_env && { echo; ok "检查通过，可执行 ./start.sh 启动"; }
        ;;
    start)  do_start ;;
esac
