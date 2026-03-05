from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter, sleep
from typing import Any
import json
from urllib.request import Request, urlopen


class OpenClawError(RuntimeError):
    pass


@dataclass
class OpenClawHealth:
    ok: bool
    status: str
    latency_ms: int
    detail: str


class OpenClawClient:
    def __init__(self, endpoint: str, timeout_sec: float = 20.0, retries: int = 2) -> None:
        self.endpoint = endpoint.strip()
        self.timeout_sec = timeout_sec
        self.retries = max(0, retries)

    def _post_json(self, payload: dict[str, Any], timeout_sec: float | None = None) -> Any:
        if not self.endpoint:
            raise OpenClawError('OPENCLAW_ENDPOINT 未配置')
        req = Request(
            self.endpoint,
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
            method='POST',
        )
        try:
            with urlopen(req, timeout=timeout_sec or self.timeout_sec) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except Exception as exc:
            raise OpenClawError(str(exc)) from exc

    def health_check(self) -> OpenClawHealth:
        if not self.endpoint:
            return OpenClawHealth(ok=False, status='not_configured', latency_ms=0, detail='OPENCLAW_ENDPOINT 未配置')

        start = perf_counter()
        try:
            payload = {'task': 'health_check', 'input': 'ping'}
            data = self._post_json(payload, timeout_sec=min(8.0, self.timeout_sec))
            latency = int((perf_counter() - start) * 1000)
            if isinstance(data, dict):
                status = str(data.get('status', 'ok')).lower()
                detail = str(data.get('detail', data.get('message', 'ok')))
            else:
                status = 'ok'
                detail = 'ok'
            return OpenClawHealth(ok=True, status=status, latency_ms=latency, detail=detail)
        except Exception as exc:
            latency = int((perf_counter() - start) * 1000)
            return OpenClawHealth(ok=False, status='down', latency_ms=latency, detail=str(exc))

    def generate_strategies(self, count: int) -> list[dict[str, Any]]:
        if not self.endpoint:
            raise OpenClawError('OPENCLAW_ENDPOINT 未配置')

        payload = {
            'task': 'generate_polymarket_live_strategies',
            'count': int(max(1, count)),
            'output_schema': {
                'strategies': [
                    {
                        'name': 'string',
                        'strategy_type': 'periodic|mean_reversion',
                        'params': {
                            'order_qty': 'float',
                            'hold_ticks': 'int',
                            'risk_loss_limit_pct': 'float',
                            'mean_rev_window': 'int?',
                            'mean_rev_threshold': 'float?',
                        },
                    }
                ]
            },
        }

        last_error = ''
        for i in range(self.retries + 1):
            try:
                data = self._post_json(payload)
                items = data.get('strategies', []) if isinstance(data, dict) else []
                if not isinstance(items, list):
                    raise OpenClawError('返回格式缺少 strategies[]')
                return items
            except Exception as exc:
                last_error = str(exc)
                if i < self.retries:
                    sleep(0.5 * (i + 1))

        raise OpenClawError(last_error or 'OpenClaw 调用失败')
