from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LiveGuardStatus:
    enabled: bool
    ready: bool
    reason: str


REQUIRED_KEYS = ['POLYMARKET_API_KEY', 'POLYMARKET_API_SECRET', 'POLYMARKET_API_PASSPHRASE']


def evaluate_live_guard(enabled: bool, env: dict[str, Any]) -> LiveGuardStatus:
    if not enabled:
        return LiveGuardStatus(enabled=False, ready=False, reason='LIVE_TRADING_ENABLED 未开启，当前仅允许模拟盘。')

    missing: list[str] = []
    for key in REQUIRED_KEYS:
        if not str(env.get(key, '')).strip():
            missing.append(key)

    if missing:
        return LiveGuardStatus(
            enabled=True,
            ready=False,
            reason=f'实盘模式已开启，但缺少密钥配置: {", ".join(missing)}',
        )

    return LiveGuardStatus(enabled=True, ready=True, reason='实盘配置检查通过。')
