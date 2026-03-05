#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib import error, parse, request


def _json_request(url: str, method: str = 'GET', payload: dict[str, Any] | None = None, timeout: float = 120.0) -> Any:
    data = None
    headers = {'Accept': 'application/json'}
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    req = request.Request(url=url, data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode('utf-8')
            return json.loads(body) if body else {}
    except error.HTTPError as exc:
        body = exc.read().decode('utf-8', errors='ignore')
        raise RuntimeError(f'HTTP {exc.code} {url}: {body[:400]}') from exc
    except error.URLError as exc:
        raise RuntimeError(f'URL error {url}: {exc}') from exc


@dataclass
class SmokeResult:
    ok: bool
    message: str
    detail: dict[str, Any]


def _expect(cond: bool, msg: str) -> None:
    if not cond:
        raise RuntimeError(msg)


def run(base_url: str) -> SmokeResult:
    base = base_url.rstrip('/')
    detail: dict[str, Any] = {}

    status = _json_request(f'{base}/api/status')
    _expect(isinstance(status, dict), '/api/status payload invalid')
    detail['live_trading_enabled'] = bool(status.get('live_trading_enabled', False))

    q_status = _json_request(f'{base}/api/quant/status')
    _expect(isinstance(q_status, dict) and 'running' in q_status, '/api/quant/status payload invalid')
    pre_order_count = int(((q_status.get('db', {}) or {}).get('counts', {}) or {}).get('q_order', 0))
    detail['quant_status_before'] = {
        'running': bool(q_status.get('running', False)),
        'phase': str(q_status.get('phase', '')),
        'q_order': pre_order_count,
    }

    refreshed = _json_request(f'{base}/api/quant/refresh-markets?limit=50&max_books=120', method='POST')
    _expect(bool(refreshed.get('ok', False)), '/api/quant/refresh-markets failed')
    detail['refresh'] = {
        'markets': int(refreshed.get('markets', 0)),
        'tokens': int(refreshed.get('tokens', 0)),
        'elapsed_sec': float(refreshed.get('elapsed_sec', 0.0)),
    }

    payload = {
        'mode': 'paper',
        'cycle_sec': 12,
        'market_limit': 80,
        'max_books': 160,
        'max_signals_per_cycle': 30,
        'enable_arb': False,
        'enable_mm': True,
        'enable_ai': False,
        'dry_run': False,
        'confirm_live': False,
        'max_order_usdc': 25,
        'max_total_exposure_usdc': 500,
        'strategy_daily_loss_limit': -50,
        'account_daily_loss_limit': -100,
        'loss_streak_limit': 5,
        'reduced_size_scale': 0.5,
        'race_enabled': False,
        'race_min_fills': 12,
        'race_min_win_rate': 0.4,
        'race_min_pnl': 0,
        'race_lookback_hours': 24,
        'mm_liq_min': 1000,
        'mm_liq_max': 120000,
        'mm_min_spread': 0.05,
        'mm_min_volume': 1000,
        'mm_min_depth_usdc': 500,
        'mm_min_market_count': 10,
        'mm_target_market_count': 12,
        'mm_max_single_side_position_usdc': 50,
        'mm_max_position_per_market_usdc': 50,
        'mm_inventory_skew_strength': 1,
        'mm_allow_short_sell': False,
        'mm_taker_rebalance': False,
        'ai_deviation_threshold': 0.10,
        'ai_min_confidence': 0.5,
        'ai_eval_interval_sec': 900,
        'ai_max_markets_per_cycle': 6,
        'arb_buy_threshold': 0.96,
        'arb_sell_threshold': 1.04,
        'fee_buffer': 0.02,
        'enforce_live_gate': True,
        'live_gate_min_hours': 72,
        'live_gate_min_win_rate': 0.45,
        'live_gate_min_pnl': 0,
        'live_gate_min_fills': 20,
    }
    one = _json_request(f'{base}/api/quant/run-once', method='POST', payload=payload, timeout=180.0)
    summary = one.get('summary', {}) if isinstance(one, dict) else {}
    _expect(isinstance(summary, dict), '/api/quant/run-once summary invalid')
    detail['run_once'] = {
        'signals_created': int(summary.get('signals_created', 0)),
        'signals_executed': int(summary.get('signals_executed', 0)),
        'signals_failed': int(summary.get('signals_failed', 0)),
        'signals_dropped_no_book': int(summary.get('signals_dropped_no_book', 0)),
    }
    if detail['run_once']['signals_created'] <= 0:
        detail['run_once']['note'] = '当前轮次未命中交易机会（严格阈值下属正常现象）'

    signals = _json_request(f'{base}/api/quant/signals?limit=200')
    orders = _json_request(f'{base}/api/quant/orders?limit=200')
    fills = _json_request(f'{base}/api/quant/fills?limit=200')
    perf = _json_request(f'{base}/api/quant/performance?mode=paper&hours=24')
    gate = _json_request(f'{base}/api/quant/live-gate')
    events = _json_request(f'{base}/api/quant/events?limit=50')

    _expect(isinstance(signals, dict) and 'rows' in signals, 'signals endpoint invalid')
    _expect(isinstance(orders, dict) and 'rows' in orders, 'orders endpoint invalid')
    _expect(isinstance(fills, dict) and 'rows' in fills, 'fills endpoint invalid')
    _expect(isinstance(perf, dict) and 'rows' in perf, 'performance endpoint invalid')
    _expect(isinstance(gate, dict) and 'rows' in gate, 'live-gate endpoint invalid')
    _expect(isinstance(events, dict) and 'rows' in events, 'events endpoint invalid')

    detail['post_counts'] = {
        'signals': int(signals.get('count', 0)),
        'orders': int(orders.get('count', 0)),
        'fills': int(fills.get('count', 0)),
        'performance_rows': int(perf.get('count', 0)),
        'gate_rows': int(gate.get('count', 0)),
        'gate_eligible': int(gate.get('eligible_count', 0)),
        'events': int(events.get('count', 0)),
    }
    post_order_count = int(((_json_request(f'{base}/api/quant/status').get('db', {}) or {}).get('counts', {}) or {}).get('q_order', 0))
    detail['post_counts']['q_order_total'] = post_order_count
    executed = int(detail['run_once']['signals_executed'])
    if int(detail['run_once']['signals_created']) > 0:
        _expect(executed > 0 or post_order_count > pre_order_count, 'run-once created signals but did not create new orders/fills')

    return SmokeResult(True, 'quant smoke test passed', detail)


def main() -> int:
    parser = argparse.ArgumentParser(description='Quant stack smoke test')
    parser.add_argument('--base-url', default='http://127.0.0.1:8780')
    args = parser.parse_args()

    started = time.time()
    try:
        out = run(args.base_url)
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False, indent=2))
        return 1

    elapsed = time.time() - started
    print(json.dumps({'ok': out.ok, 'message': out.message, 'elapsed_sec': round(elapsed, 3), 'detail': out.detail}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
