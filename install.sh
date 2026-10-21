#!/usr/bin/env bash
# llm_server_eval 环境安装脚本
#
# 用法：
#   ./install.sh                 # 装核心依赖（后端 + 前端构建）
#   ./install.sh --all           # 再加上压测引擎与 Agent 评测依赖
#   ./install.sh --perf          # 只加压测引擎 evalscope[perf]
#   ./install.sh --agent         # 只加 Agent 评测依赖（uv + pier + docker 检查）
#   ./install.sh --no-frontend   # 不动前端（后端已有 backend/webui 产物时够用）
#   ./install.sh --dry-run       # 只打印将要执行的命令
#
# 可选项：--venv PATH（默认 .venv）、--python PATH（默认 python3）
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$ROOT"

# pier 会去 GitHub 拉模型价目表，内网下每次 `pier --version` 要 20s+，这里统一关掉
export LITELLM_LOCAL_MODEL_COST_MAP="${LITELLM_LOCAL_MODEL_COST_MAP:-true}"

# uv tool（pier）装到 ~/.local/bin、pip 装进本项目 venv 的脚本落在 .venv/bin，
# 未激活虚拟环境时都不在 PATH 里，自检会误报“未安装”，这里统一补上
export PATH="$HOME/.local/bin:$PATH"

WITH_PERF=0
WITH_AGENT=0
BUILD_FRONTEND=1
DRY_RUN=0
VENV="$ROOT/.venv"
PYTHON_BIN="${PYTHON:-python3}"

info() { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
ok()   { printf '  \033[0;32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[0;33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[0;31m[错误]\033[0m %s\n' "$*" >&2; exit 1; }
run()  {
    if [ "$DRY_RUN" = 1 ]; then printf '  + %s\n' "$*"; return 0; fi
    "$@"
}

usage() {
    # 打印脚本头部注释（首个非注释行为止）
    awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}"
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --all)          WITH_PERF=1; WITH_AGENT=1 ;;
        --perf)         WITH_PERF=1 ;;
        --agent)        WITH_AGENT=1 ;;
        --no-frontend)  BUILD_FRONTEND=0 ;;
        --dry-run)      DRY_RUN=1 ;;
        --venv)         VENV="${2:?--venv 需要路径}"; shift ;;
        --python)       PYTHON_BIN="${2:?--python 需要路径}"; shift ;;
        -h|--help)      usage ;;
        *)              die "未知参数：$1（--help 查看用法）" ;;
    esac
    shift
done

# ------------------------------------------------------------------ 系统检查
check_python() {
    command -v "$PYTHON_BIN" >/dev/null 2>&1 \
        || die "找不到解释器 '$PYTHON_BIN'，请先安装 Python 3.10+（或用 --python 指定）"
    if ! "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
        die "Python 版本过低（$("$PYTHON_BIN" -V 2>&1)），需要 3.10+"
    fi
    ok "Python $("$PYTHON_BIN" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
}

check_node() {
    if ! command -v node >/dev/null 2>&1; then
        die "构建前端需要 Node.js 18+；已有 backend/webui 产物时可用 --no-frontend 跳过"
    fi
    local major
    major=$(node -p 'process.versions.node.split(".")[0]')
    [ "$major" -ge 18 ] || die "Node.js 版本过低（$(node -v)），Vite 6 需要 18+"
    ok "Node.js $(node -v) / npm $(npm -v)"
}

# ------------------------------------------------------------------ 各步骤
ensure_venv() {
    if [ -x "$VENV/bin/python" ]; then
        ok "复用虚拟环境 $VENV"
    else
        info "创建虚拟环境 $VENV"
        run "$PYTHON_BIN" -m venv "$VENV" \
            || die "创建 venv 失败；Debian/Ubuntu 需先 sudo apt install python3-venv"
    fi
    PY="$VENV/bin/python"
    run "$PY" -m pip install --quiet --disable-pip-version-check --upgrade pip setuptools wheel
}

install_deps() {
    info "安装后端依赖（requirements.txt）"
    run "$PY" -m pip install --quiet --disable-pip-version-check -r requirements.txt
    ok "fastapi / uvicorn / pandas / pyarrow / tiktoken / pyyaml"
}

# 与后端 perf_core.resolve_bin 一致：EVALSCOPE_BIN > PATH > venv/bin > 常见位置
evalscope_path() {
    if [ -n "${EVALSCOPE_BIN:-}" ] && [ -x "${EVALSCOPE_BIN}" ]; then
        printf '%s\n' "$EVALSCOPE_BIN"; return 0
    fi
    local bin
    if bin=$(command -v evalscope 2>/dev/null); then
        printf '%s\n' "$bin"; return 0
    fi
    for bin in "$VENV/bin/evalscope" "$HOME/.local/bin/evalscope" "/usr/local/bin/evalscope"; do
        if [ -x "$bin" ]; then printf '%s\n' "$bin"; return 0; fi
    done
    return 1
}

evalscope_available() { evalscope_path >/dev/null 2>&1; }

install_perf() {
    if evalscope_available; then
        ok "已存在 evalscope（$(evalscope_path)），跳过安装"
        return 0
    fi
    info "安装压测引擎 evalscope[perf]（依赖较重，首次约数百 MB）"
    run "$PY" -m pip install --quiet --disable-pip-version-check 'evalscope[perf]'
    if [ "$DRY_RUN" = 1 ]; then
        printf '  + test -x %s/bin/evalscope\n' "$VENV"
        return 0
    fi
    evalscope_available \
        || die "evalscope 安装后仍找不到可执行文件（查看 $VENV/bin/evalscope，或用 EVALSCOPE_BIN 指定）"
    ok "evalscope $(evalscope_path)（压测可用）"
}

install_agent() {
    info "检查 Agent 评测依赖（pier / docker）"

    # uv：pier 通过 uv tool 安装，可执行文件落在 ~/.local/bin（与后端查找逻辑一致）
    export PATH="$HOME/.local/bin:$VENV/bin:$PATH"
    if command -v uv >/dev/null 2>&1; then
        ok "uv $(uv --version 2>/dev/null | head -1)"
    else
        run "$PY" -m pip install --quiet --disable-pip-version-check uv
        export PATH="$VENV/bin:$PATH"
        ok "uv 已安装到 $VENV/bin"
    fi

    if command -v pier >/dev/null 2>&1; then
        ok "pier $(pier --version 2>/dev/null | head -1)（$(command -v pier)）"
    else
        info "安装 datacurve-pier（uv tool install）"
        run uv tool install datacurve-pier
        ok "pier 已安装到 ~/.local/bin/pier"
        warn "pier 的本地适配补丁（egress 端口、uv-base）不随安装自动应用，详见 develop.md"
    fi

    # docker：直连失败时后端会自动退回 sg docker，这里只做提示
    if docker info >/dev/null 2>&1; then
        ok "docker $(docker info --format '{{.ServerVersion}}' 2>/dev/null)"
    elif sg docker -c 'docker info' >/dev/null 2>&1; then
        ok "docker 可用（当前会话无 docker 组权限，服务会自动用 sg docker）"
    else
        warn "docker 不可用：Agent 评测需要 docker 拉沙箱"
        warn "  安装后执行：sudo usermod -aG docker \$USER && newgrp docker"
    fi

    if docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null \
            | grep -qx 'llm-server-eval/uv-base:0.7.13'; then
        ok "基础镜像 llm-server-eval/uv-base:0.7.13 存在"
    else
        warn "缺少基础镜像 llm-server-eval/uv-base:0.7.13：Agent 任务镜像构建会失败"
        warn "  重建命令见 develop.md 的「重建 uv-base」一节"
    fi
}

install_frontend() {
    info "安装前端依赖并构建"
    if [ -f frontend/package-lock.json ]; then
        run npm --prefix frontend ci --no-audit --no-fund \
            || run npm --prefix frontend install --no-audit --no-fund
    else
        run npm --prefix frontend install --no-audit --no-fund
    fi
    # 先清空产物目录避免旧 chunk 残留；受限环境删不掉时退回覆盖构建
    if [ "$DRY_RUN" = 1 ]; then
        printf '  + rm -rf %s\n' "$ROOT/backend/webui"
        printf '  + npm --prefix frontend run build\n'
    elif rm -rf "$ROOT/backend/webui" 2>/dev/null; then
        npm --prefix frontend run build
    else
        warn "产物目录无法清空（受限环境），改为覆盖构建"
        npm --prefix frontend run build -- --no-emptyOutDir
    fi
    [ -f backend/webui/index.html ] || die "前端构建后缺少 backend/webui/index.html"
    ok "前端产物已输出到 backend/webui/"
}

selfcheck() {
    info "自检"
    "$PY" - <<'PY'
import importlib.util
import sys

mods = ["fastapi", "uvicorn", "pandas", "pyarrow", "tiktoken", "yaml", "requests"]
missing = [m for m in mods if importlib.util.find_spec(m) is None]
if missing:
    print("  \033[0;31m✗\033[0m 缺少 Python 依赖: " + ", ".join(missing))
    sys.exit(1)
print("  \033[0;32m✓\033[0m 后端依赖齐全")
PY
    # evalscope 可能来自：EVALSCOPE_BIN、PATH（uv tool 装到 ~/.local/bin）、
    # 或 pip 装进本项目 venv 的 bin 目录
    if evalscope_bin=$(evalscope_path); then
        ok "evalscope（${evalscope_bin}，压测可用）"
    else
        warn "evalscope 未安装：压测不可用（./install.sh --perf）"
    fi
    command -v pier >/dev/null 2>&1 \
        && ok "pier $(pier --version 2>/dev/null | head -1)（Agent 评测可用）" \
        || warn "pier 未安装：Agent 评测不可用（./install.sh --agent）"
}

# ------------------------------------------------------------------ 主流程
info "安装目录：$ROOT"
check_python
[ "$BUILD_FRONTEND" = 1 ] && check_node
ensure_venv
install_deps
[ "$WITH_PERF" = 1 ] && install_perf
[ "$WITH_AGENT" = 1 ] && install_agent
[ "$BUILD_FRONTEND" = 1 ] && install_frontend
selfcheck

echo
info "安装完成"
cat <<EOF
  启动服务： ./start.sh                    # 前台，默认 0.0.0.0:8000
             PORT=8010 ./start.sh          # 换端口
             ./start.sh --daemon          # 后台，日志 runs/server.log
             ./start.sh --stop            # 停止后台服务
             ./start.sh --check           # 只做启动前检查
EOF
if ! evalscope_available; then
    echo "  压测功能不可用：./install.sh --perf"
fi
if ! command -v pier >/dev/null 2>&1; then
    echo "  Agent 评测不可用：./install.sh --agent"
fi
