#!/usr/bin/env bash
# 一键打包：前端构建 -> PyInstaller 单文件 -> 组装发布目录 -> tar.gz
#
#   ./packaging/build.sh               # 含 dataset/ model/（默认可直接对外分发）
#   ./packaging/build.sh --no-data     # 只打包程序，数据另行准备
#   ./packaging/build.sh --skip-web    # 复用已有 backend/webui 产物
#   ./packaging/build.sh --version 1.2 # 自定义发布版本号
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME="llm-server-eval"
VERSION="$(date +%Y%m%d)"
WITH_DATA=1
SKIP_WEB=0

usage() {
  sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --no-data) WITH_DATA=0 ;;
    --skip-web) SKIP_WEB=1 ;;
    --version) VERSION="${2:?--version 需要参数}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage; exit 1 ;;
  esac
  shift
done

STAGE="$ROOT/dist/$NAME-$VERSION"
TARBALL="$ROOT/dist/$NAME-linux-x86_64-$VERSION.tar.gz"

echo "==> 1/4 前端构建"
if [ "$SKIP_WEB" = 1 ]; then
  echo "    跳过（复用 backend/webui）"
else
  (cd "$ROOT/frontend" && npm run build)
fi
if [ ! -f "$ROOT/backend/webui/index.html" ]; then
  echo "缺少前端产物 backend/webui/index.html，先执行 npm run build" >&2
  exit 1
fi

echo "==> 2/4 PyInstaller 打包"
cd "$ROOT"
rm -f "$ROOT/dist/$NAME"
python3 -m PyInstaller packaging/llm_server_eval.spec --noconfirm \
  --distpath "$ROOT/dist" --workpath "$ROOT/build/pyinstaller"

echo "==> 3/4 组装发布目录 $STAGE"
rm -rf "$STAGE"
mkdir -p "$STAGE/runs"
cp "$ROOT/dist/$NAME" "$STAGE/"
chmod +x "$STAGE/$NAME"
cp "$ROOT/packaging/start.sh" "$STAGE/"
chmod +x "$STAGE/start.sh"
cp "$ROOT/packaging/README.md" "$STAGE/"

if [ "$WITH_DATA" = 1 ]; then
  cp -r "$ROOT/dataset" "$STAGE/dataset"
  cp -r "$ROOT/model" "$STAGE/model"
else
  mkdir -p "$STAGE/dataset" "$STAGE/model"
  echo "    未包含 dataset/ model/，需自行放入数据后启动"
fi

echo "==> 4/4 生成压缩包"
rm -f "$TARBALL"
tar -czf "$TARBALL" -C "$ROOT/dist" "$NAME-$VERSION"

echo
echo "完成："
echo "  目录  $STAGE"
echo "  压缩包 $TARBALL ($(du -h "$TARBALL" | cut -f1))"
echo "  可执行 $STAGE/$NAME ($(du -h "$STAGE/$NAME" | cut -f1))"
