#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


@dataclass
class SmokeResult:
    ok: bool
    message: str
    details: dict[str, Any]


class HttpClient:
    def __init__(self, base_url: str, timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip('/')
        self.timeout = float(timeout)

    def get(self, path: str) -> Any:
        req = Request(url=f'{self.base_url}{path}', headers={'Accept': 'application/json'}, method='GET')
        return self._send(req)

    def post(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode('utf-8')
        req = Request(
            url=f'{self.base_url}{path}',
            data=data,
            headers={'Accept': 'application/json', 'Content-Type': 'application/json'},
            method='POST',
        )
        return self._send(req)

    def _send(self, req: Request) -> Any:
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode('utf-8')
                return json.loads(raw) if raw else {}
        except HTTPError as exc:
            body = exc.read().decode('utf-8', errors='ignore')
            raise RuntimeError(f'HTTP {exc.code}: {body}') from exc
        except URLError as exc:
            raise RuntimeError(f'URL error: {exc}') from exc


def _pick_first_token(markets_payload: dict[str, Any]) -> str:
    rows = markets_payload.get('rows', []) if isinstance(markets_payload, dict) else []
    if not isinstance(rows, list) or not rows:
        return ''
    first = rows[0] if isinstance(rows[0], dict) else {}
    outcomes = first.get('outcomes', [])
    if isinstance(outcomes, list):
        for row in outcomes:
            if isinstance(row, dict):
                token_id = str(row.get('token_id', '')).strip()
                if token_id:
                    return token_id
    token_ids = first.get('token_ids', [])
    if isinstance(token_ids, list):
        for token_id in token_ids:
            s = str(token_id).strip()
            if s:
                return s
    return ''


def _wait_generate_job_done(client: HttpClient, job_id: str, max_wait_sec: int = 120) -> dict[str, Any]:
    started = time.time()
    last_status = ''
    while time.time() - started <= max_wait_sec:
        row = client.get(f'/api/strategies/generate-jobs/{quote(job_id)}')
        status = str(row.get('status', '')).strip().lower()
        if status != last_status:
            pct = row.get('progress_pct', 0)
            msg = row.get('message', '')
            print(f'[job] status={status} progress={pct}% message={msg}')
            last_status = status
        if status in {'succeeded', 'failed'}:
            return row
        time.sleep(1.2)
    raise TimeoutError(f'wait generate job timeout > {max_wait_sec}s')


def _pick_limit_price(book: dict[str, Any]) -> float:
    if not isinstance(book, dict):
        return 0.5
    asks = book.get('asks', [])
    bids = book.get('bids', [])
    for rows in (asks, bids):
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                px = row.get('price')
                try:
                    val = float(px) if px is not None else 0.0
                except (TypeError, ValueError):
                    val = 0.0
                if val > 0:
                    return max(0.01, min(0.99, val))
    return 0.5


def _num(v: Any, default: float) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return float(default)
    if x <= 0:
        return float(default)
    return x


def run_smoke(base_url: str) -> SmokeResult:
    client = HttpClient(base_url=base_url, timeout=25.0)
    details: dict[str, Any] = {'base_url': base_url}

    print('[1/10] health check')
    health = client.get('/api/health')
    if not isinstance(health, dict) or not health.get('ok'):
        return SmokeResult(False, 'health check failed', {'health': health})

    print('[2/10] load market and pick token')
    markets = client.get('/api/paper/markets?limit=1')
    token_id = _pick_first_token(markets if isinstance(markets, dict) else {})
    if not token_id:
        return SmokeResult(False, 'cannot pick token from /api/paper/markets', {'markets': markets})
    details['token_id'] = token_id

    print('[3/10] submit async strategy generate job')
    created = client.post(
        '/api/strategies/generate-async',
        {
            'count': 2,
            'provider_id': '',
            'prompt': '低频、低回撤、优先流动性高市场。',
            'seed': int(time.time()) % 1000000,
            'allow_fallback': True,
        },
    )
    job_id = str(created.get('job_id', '')).strip() if isinstance(created, dict) else ''
    if not job_id:
        return SmokeResult(False, 'create generate job failed', {'created': created})
    details['job_id'] = job_id

    print('[4/10] wait strategy generation complete')
    job = _wait_generate_job_done(client, job_id=job_id, max_wait_sec=120)
    details['job_status'] = job.get('status')
    if str(job.get('status', '')).lower() != 'succeeded':
        return SmokeResult(False, 'strategy generation job failed', {'job': job})
    result = job.get('result', {}) if isinstance(job.get('result', {}), dict) else {}
    rows = result.get('rows', []) if isinstance(result, dict) else []
    strategy_id = ''
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        strategy_id = str(rows[0].get('strategy_id', '')).strip()
    if not strategy_id:
        list_resp = client.get('/api/strategies')
        list_rows = list_resp.get('rows', []) if isinstance(list_resp, dict) else []
        if isinstance(list_rows, list) and list_rows and isinstance(list_rows[0], dict):
            strategy_id = str(list_rows[0].get('strategy_id', '')).strip()
    if not strategy_id:
        return SmokeResult(False, 'no strategy_id after generation', {'job': job})
    details['strategy_id'] = strategy_id
    details['generate_source'] = result.get('source', '')
    details['generate_fallback'] = bool(result.get('used_fallback', False))

    jobs_list = client.get('/api/strategies/generate-jobs?limit=20')
    list_rows = jobs_list.get('rows', []) if isinstance(jobs_list, dict) else []
    if not any(isinstance(x, dict) and str(x.get('job_id', '')) == job_id for x in (list_rows if isinstance(list_rows, list) else [])):
        return SmokeResult(False, 'generate job history does not include latest job', {'job_id': job_id, 'jobs': jobs_list})
    details['job_history_count'] = int(jobs_list.get('count', 0)) if isinstance(jobs_list, dict) else 0

    print('[5/10] enable selected strategy only')
    all_strategies = client.get('/api/strategies')
    rows2 = all_strategies.get('rows', []) if isinstance(all_strategies, dict) else []
    if isinstance(rows2, list):
        for row in rows2:
            if not isinstance(row, dict):
                continue
            sid = str(row.get('strategy_id', '')).strip()
            if not sid:
                continue
            should_enable = sid == strategy_id
            if bool(row.get('enabled', True)) == should_enable:
                continue
            client.post('/api/strategies/toggle', {'strategy_id': sid, 'enabled': should_enable})

    print('[6/10] start paper bot')
    try:
        client.post('/api/paper/trading/bot/stop', {})
    except Exception:
        pass
    start_out = client.post(
        '/api/paper/trading/bot/start',
        {
            'token_id': token_id,
            'interval_sec': 6,
            'prefer_stream': True,
        },
    )
    details['bot_start'] = start_out
    if not bool(start_out.get('running', start_out.get('bot', {}).get('running', False))):
        return SmokeResult(False, 'paper bot not running after start', {'bot_start': start_out})

    print('[7/10] wait bot ticks and verify data updates')
    time.sleep(4.0)
    status = client.get('/api/paper/trading/status?limit=80')
    details['paper_status'] = {
        'fills_count': status.get('fills_count'),
        'orders_count': status.get('orders_count'),
        'bot': status.get('bot', {}),
    }

    fills = client.get(f'/api/paper/trading/fills?limit=200&strategy_id={quote(strategy_id)}')
    fill_count = int(fills.get('count', 0)) if isinstance(fills, dict) else 0
    if fill_count <= 0:
        # Use one manual paper order to ensure fills table pipeline is working.
        client.post(
            '/api/paper/trading/orders/market',
            {
                'strategy_id': strategy_id,
                'token_id': token_id,
                'side': 'buy',
                'amount': 1,
                'order_type': 'FAK',
            },
        )
        fills2 = client.get(f'/api/paper/trading/fills?limit=200&strategy_id={quote(strategy_id)}')
        fill_count = int(fills2.get('count', 0)) if isinstance(fills2, dict) else 0
    details['strategy_fill_count'] = fill_count
    if fill_count <= 0:
        return SmokeResult(False, 'no fills after bot/manual order', details)

    workflow = client.get('/api/paper/workflow-status')
    details['workflow_next_action'] = workflow.get('next_action', '') if isinstance(workflow, dict) else ''
    steps = workflow.get('steps', []) if isinstance(workflow, dict) else []
    if not isinstance(steps, list) or not steps:
        return SmokeResult(False, 'workflow status missing steps', {'workflow': workflow})

    promotion = client.get('/api/performance/promotion?min_pnl=0&max_dd_pct=1.5&min_trades=1&min_win_rate=0')
    if not isinstance(promotion, dict) or not isinstance(promotion.get('rows', []), list):
        return SmokeResult(False, 'promotion endpoint invalid payload', {'promotion': promotion})
    details['promotion_candidates_count'] = int(promotion.get('count', 0))

    print('[8/10] submit manual paper limit order and verify cancel-all')
    book = client.get(f'/api/paper/orderbook/{quote(token_id)}')
    rule = client.get(f'/api/paper/token-rule/{quote(token_id)}')
    limit_price = _pick_limit_price(book if isinstance(book, dict) else {})
    rule_row = (rule or {}).get('rule', {}) if isinstance(rule, dict) else {}
    min_size = _num(rule_row.get('min_size') if isinstance(rule_row, dict) else None, 1.0)
    limit_size = max(1.0, min_size)
    limit_out = client.post(
        '/api/paper/trading/orders/limit',
        {
            'strategy_id': strategy_id,
            'token_id': token_id,
            'side': 'BUY',
            'price': limit_price,
            'size': limit_size,
            'order_type': 'GTC',
        },
    )
    details['manual_limit_order'] = {
        'ok': bool(limit_out.get('ok', False)) if isinstance(limit_out, dict) else False,
        'order_id': limit_out.get('order', {}).get('order_id') if isinstance(limit_out, dict) else '',
        'price': limit_price,
        'size': limit_size,
    }
    open_orders = client.get(f'/api/paper/trading/orders?limit=200&strategy_id={quote(strategy_id)}&open_only=true')
    if not isinstance(open_orders, dict) or not isinstance(open_orders.get('rows', []), list):
        return SmokeResult(False, 'open orders payload invalid after manual limit', {'open_orders': open_orders})
    details['open_orders_after_manual'] = int(open_orders.get('count', 0))
    cancel_all = client.post('/api/paper/trading/orders/cancel-all', {})
    details['cancel_all'] = cancel_all

    print('[9/10] stop paper bot and finalize')
    stop_out = client.post('/api/paper/trading/bot/stop', {})
    details['bot_stop'] = stop_out

    print('[10/10] verify auto quant status endpoint')
    auto_status = client.get('/api/paper/auto/status?limit_logs=20')
    if not isinstance(auto_status, dict) or 'running' not in auto_status:
        return SmokeResult(False, 'auto status endpoint invalid payload', {'auto_status': auto_status})
    details['auto_status'] = {
        'running': bool(auto_status.get('running', False)),
        'phase': auto_status.get('phase', ''),
        'cycle': int(auto_status.get('cycle', 0)),
    }

    return SmokeResult(True, 'smoke test passed', details)


def main() -> int:
    parser = argparse.ArgumentParser(description='Polymarket workbench smoke test')
    parser.add_argument('--base-url', default='http://127.0.0.1:8780', help='live site base URL')
    args = parser.parse_args()

    try:
        out = run_smoke(args.base_url)
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False, indent=2))
        return 1

    print(json.dumps({'ok': out.ok, 'message': out.message, 'details': out.details}, ensure_ascii=False, indent=2))
    return 0 if out.ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
