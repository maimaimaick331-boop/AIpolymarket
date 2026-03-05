#!/bin/zsh
set -euo pipefail

cd "$(dirname "$0")"

echo "[启动] Polymarket 交易中台..."
( sleep 3; open "http://127.0.0.1:8780/paper" >/dev/null 2>&1 ) &

./run_live_site.sh
