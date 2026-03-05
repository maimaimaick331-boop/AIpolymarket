from __future__ import annotations

import os
from pathlib import Path
import sys

if __package__ in (None, ''):
    # Allow running as: python apps/trader/run_live_guard_check.py
    sys.path.append(str(Path(__file__).resolve().parents[2]))

from libs.core.config import load_settings
from libs.core.live_guard import evaluate_live_guard


def main() -> int:
    settings = load_settings()
    status = evaluate_live_guard(settings.live_trading_enabled, os.environ)

    print('=== 实盘安全网关检查 ===')
    print(f'实盘开关: {"开启" if status.enabled else "关闭"}')
    print(f'是否可进入实盘流程: {"是" if status.ready else "否"}')
    print(f'说明: {status.reason}')

    if status.ready:
        print('\n注意：当前仓库仍未执行真实下单逻辑，请先完成签名下单与风控联调。')
        return 0
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
