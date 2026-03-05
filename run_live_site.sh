#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "[1/5] 检查环境文件"
if [ ! -f .env ] && [ -f .env.example ]; then
  cp .env.example .env
fi

echo "[2/5] 检查 Python 虚拟环境"
if [ ! -d .venv ]; then
  python -m venv .venv
fi

echo "[3/5] 释放 8780 端口（避免跑到旧版本服务）"
pids="$(lsof -tiTCP:8780 -sTCP:LISTEN 2>/dev/null || true)"
if [ -n "${pids}" ]; then
  echo "发现占用进程: ${pids}"
  # shellcheck disable=SC2086
  kill ${pids} >/dev/null 2>&1 || true
  sleep 1
  for pid in ${pids}; do
    if kill -0 "${pid}" >/dev/null 2>&1; then
      kill -9 "${pid}" >/dev/null 2>&1 || true
    fi
  done
fi

echo "[4/5] 安装 Python 依赖"
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements-live.txt

if command -v npm >/dev/null 2>&1; then
  echo "[4.5/5] 构建前端仪表盘"
  (
    cd apps/web/dashboard-app
    npm run build --silent
  )
else
  echo "[4.5/5] 跳过前端构建（未检测到 npm）"
fi

echo "[5/5] 启动服务: http://127.0.0.1:8780"
exec .venv/bin/python apps/web/run_live_site.py
