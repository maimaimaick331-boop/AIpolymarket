from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json
import random
from urllib.request import Request, urlopen
from libs.services.openclaw_client import OpenClawClient


@dataclass
class StrategyConfig:
    strategy_id: str
    name: str
    strategy_type: str
    params: dict[str, Any]
    enabled: bool
    source: str
    created_at_utc: str


class LiveStrategyStore:
    def __init__(self, store_dir: Path) -> None:
        self.store_dir = store_dir
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.strategies_file = self.store_dir / 'strategies.json'
        self.log_file = self.store_dir / 'strategy_logs.jsonl'

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def load_strategies(self) -> list[StrategyConfig]:
        if not self.strategies_file.exists():
            return []
        try:
            data = json.loads(self.strategies_file.read_text(encoding='utf-8'))
        except Exception:
            return []
        out: list[StrategyConfig] = []
        if isinstance(data, list):
            for row in data:
                if not isinstance(row, dict):
                    continue
                out.append(
                    StrategyConfig(
                        strategy_id=str(row.get('strategy_id', '')),
                        name=str(row.get('name', '')),
                        strategy_type=str(row.get('strategy_type', 'periodic')),
                        params=row.get('params', {}) if isinstance(row.get('params'), dict) else {},
                        enabled=bool(row.get('enabled', True)),
                        source=str(row.get('source', 'template')),
                        created_at_utc=str(row.get('created_at_utc', self._now())),
                    )
                )
        return out

    def save_strategies(self, strategies: list[StrategyConfig]) -> None:
        self.strategies_file.write_text(
            json.dumps([asdict(s) for s in strategies], ensure_ascii=False, indent=2),
            encoding='utf-8',
        )

    def append_log(self, payload: dict[str, Any]) -> None:
        line = {'time_utc': self._now(), **payload}
        with self.log_file.open('a', encoding='utf-8') as fp:
            fp.write(json.dumps(line, ensure_ascii=False))
            fp.write('\n')

    def read_logs(self, limit: int = 200) -> list[dict[str, Any]]:
        if not self.log_file.exists():
            return []
        lines = self.log_file.read_text(encoding='utf-8').splitlines()[-max(1, limit) :]
        out: list[dict[str, Any]] = []
        for line in lines:
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out


def generate_template_strategies(n: int, seed: int = 20260304) -> list[StrategyConfig]:
    rng = random.Random(seed)
    out: list[StrategyConfig] = []
    now = datetime.now(timezone.utc).isoformat()
    for i in range(max(1, n)):
        if i % 2 == 0:
            st = StrategyConfig(
                strategy_id=f'live-{i+1:03d}',
                name=f'LivePeriodic-{i+1}',
                strategy_type='periodic',
                params={
                    'order_qty': round(rng.uniform(2.0, 12.0), 2),
                    'hold_ticks': rng.randint(1, 5),
                    'risk_loss_limit_pct': round(rng.uniform(0.5, 2.5), 2),
                },
                enabled=True,
                source='template',
                created_at_utc=now,
            )
        else:
            st = StrategyConfig(
                strategy_id=f'live-{i+1:03d}',
                name=f'LiveMeanRev-{i+1}',
                strategy_type='mean_reversion',
                params={
                    'order_qty': round(rng.uniform(2.0, 8.0), 2),
                    'hold_ticks': rng.randint(1, 4),
                    'risk_loss_limit_pct': round(rng.uniform(0.5, 2.0), 2),
                    'mean_rev_window': rng.randint(4, 12),
                    'mean_rev_threshold': round(rng.uniform(0.008, 0.03), 4),
                },
                enabled=True,
                source='template',
                created_at_utc=now,
            )
        out.append(st)
    return out


def generate_openclaw_strategies(endpoint: str, count: int, timeout_sec: float = 20.0) -> list[StrategyConfig]:
    items = OpenClawClient(endpoint=endpoint, timeout_sec=timeout_sec, retries=2).generate_strategies(count=count)
    return build_strategy_configs(items, source='openclaw')


def _extract_json_block(text: str) -> dict[str, Any]:
    s = (text or '').strip()
    if not s:
        return {}
    try:
        return json.loads(s)
    except Exception:
        pass
    left = s.find('{')
    right = s.rfind('}')
    if left >= 0 and right > left:
        try:
            return json.loads(s[left : right + 1])
        except Exception:
            return {}
    return {}


def generate_openai_compatible_strategies(
    endpoint: str,
    count: int,
    timeout_sec: float = 20.0,
    model: str = '',
    api_key: str = '',
    extra_headers: dict[str, str] | None = None,
    source: str = 'openai',
    prompt: str = '',
) -> list[StrategyConfig]:
    chat_endpoint = endpoint.rstrip('/')
    if not chat_endpoint.endswith('/v1/chat/completions'):
        chat_endpoint = f'{chat_endpoint}/v1/chat/completions'
    payload = {
        'model': model or 'local-model',
        'temperature': 0.6,
        'messages': [
            {
                'role': 'system',
                'content': (
                    '你是量化策略生成器。只输出 JSON，不要 markdown。'
                    '格式: {"strategies":[{"name":"...","strategy_type":"periodic|mean_reversion","params":{...}}]}'
                ),
            },
            {
                'role': 'user',
                'content': (
                    f'生成 {max(1, int(count))} 个 Polymarket 实盘候选策略。'
                    '参数包含 order_qty, hold_ticks, risk_loss_limit_pct，'
                    'mean_reversion 再加 mean_rev_window, mean_rev_threshold。'
                    f"{(' 用户补充要求：' + prompt.strip()) if str(prompt or '').strip() else ''}"
                ),
            },
        ],
    }
    req = Request(
        chat_endpoint,
        data=json.dumps(payload).encode('utf-8'),
        headers={
            **(
                {'Authorization': f'Bearer {api_key.strip()}'}
                if (api_key or '').strip()
                else {}
            ),
            **({k: v for k, v in (extra_headers or {}).items() if str(k).strip() and str(v).strip()}),
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        },
        method='POST',
    )
    with urlopen(req, timeout=timeout_sec) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    content = ''
    if isinstance(data, dict):
        choices = data.get('choices', [])
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            msg = choices[0].get('message', {})
            if isinstance(msg, dict):
                content = str(msg.get('content', ''))
    parsed = _extract_json_block(content)
    items = parsed.get('strategies', []) if isinstance(parsed, dict) else []
    if not isinstance(items, list):
        items = []
    return build_strategy_configs(items, source=source or 'openai')


def build_strategy_configs(items: list[dict[str, Any]], source: str) -> list[StrategyConfig]:
    out: list[StrategyConfig] = []
    now = datetime.now(timezone.utc).isoformat()
    for i, row in enumerate(items):
        if not isinstance(row, dict):
            continue
        st = str(row.get('strategy_type', '')).strip().lower()
        if st not in {'periodic', 'mean_reversion'}:
            continue
        params = row.get('params', {}) if isinstance(row.get('params'), dict) else {}
        out.append(
            StrategyConfig(
                strategy_id=f'live-ai-{i+1:03d}',
                name=str(row.get('name') or f'AI-{st}-{i+1}'),
                strategy_type=st,
                params=params,
                enabled=True,
                source=source,
                created_at_utc=now,
            )
        )
    return out


def generate_model_strategies(
    endpoint: str,
    count: int,
    timeout_sec: float = 20.0,
    adapter: str = 'openclaw_compatible',
    model: str = '',
    api_key: str = '',
    extra_headers: dict[str, str] | None = None,
    company: str = '',
    prompt: str = '',
) -> list[StrategyConfig]:
    ad = (adapter or '').strip().lower()
    if ad == 'openai_compatible':
        return generate_openai_compatible_strategies(
            endpoint=endpoint,
            count=count,
            timeout_sec=timeout_sec,
            model=model,
            api_key=api_key,
            extra_headers=extra_headers,
            source=company or 'openai',
            prompt=prompt,
        )
    return generate_openclaw_strategies(
        endpoint=endpoint,
        count=count,
        timeout_sec=timeout_sec,
    )
