from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


@dataclass
class PerfRow:
    strategy_id: str
    trades: int
    turnover: float
    realized_pnl: float
    max_drawdown_pct: float
    win_rate: float
    wins: int
    losses: int
    net_position: float
    avg_cost: float
    last_time_utc: str


class LivePerformanceService:
    def __init__(self, logs: list[dict[str, Any]]) -> None:
        self.logs = logs

    @staticmethod
    def _extract_trade(log: dict[str, Any]) -> dict[str, Any] | None:
        kind = str(log.get('kind', ''))

        if kind == 'bot_order':
            strategy_id = str(log.get('strategy_id', ''))
            side = str(log.get('signal', '')).lower()
            price = _safe_float(log.get('price'))
            size = _safe_float(log.get('size'))
            if not strategy_id or side not in {'buy', 'sell'} or price <= 0 or size <= 0:
                return None
            return {
                'strategy_id': strategy_id,
                'side': side,
                'price': price,
                'size': size,
                'time_utc': str(log.get('time_utc', '')),
            }

        if kind in {'limit_order', 'market_order'}:
            req = log.get('request', {})
            if not isinstance(req, dict):
                return None
            strategy_id = str(log.get('strategy_id', '') or req.get('strategy_id', ''))
            side = str(req.get('side', '')).lower()
            price = _safe_float(req.get('price'))
            size = _safe_float(req.get('size'))
            if size <= 0 and req.get('amount') is not None and price > 0:
                size = _safe_float(req.get('amount')) / price
            if not strategy_id or side not in {'buy', 'sell'} or price <= 0 or size <= 0:
                return None
            return {
                'strategy_id': strategy_id,
                'side': side,
                'price': price,
                'size': size,
                'time_utc': str(log.get('time_utc', '')),
            }

        if kind == 'paper_fill':
            strategy_id = str(log.get('strategy_id', ''))
            side = str(log.get('side', '')).lower()
            price = _safe_float(log.get('price'))
            size = _safe_float(log.get('size'))
            if not strategy_id or side not in {'buy', 'sell'} or price <= 0 or size <= 0:
                return None
            return {
                'strategy_id': strategy_id,
                'side': side,
                'price': price,
                'size': size,
                'time_utc': str(log.get('time_utc', '')),
            }

        return None

    def compute(self) -> list[PerfRow]:
        state: dict[str, dict[str, Any]] = {}

        for log in self.logs:
            trade = self._extract_trade(log)
            if trade is None:
                continue

            sid = trade['strategy_id']
            s = state.setdefault(
                sid,
                {
                    'pos': 0.0,
                    'avg_cost': 0.0,
                    'realized': 0.0,
                    'trades': 0,
                    'turnover': 0.0,
                    'wins': 0,
                    'losses': 0,
                    'peak': 0.0,
                    'max_dd': 0.0,
                    'last_time_utc': '',
                },
            )

            side = trade['side']
            price = trade['price']
            size = trade['size']
            pos = s['pos']
            avg = s['avg_cost']

            s['trades'] += 1
            s['turnover'] += price * size
            s['last_time_utc'] = trade['time_utc']

            if side == 'buy':
                new_pos = pos + size
                if new_pos > 1e-12:
                    s['avg_cost'] = (avg * pos + price * size) / new_pos
                s['pos'] = new_pos
            else:
                close_qty = min(max(pos, 0.0), size)
                unit_pnl = price - avg
                pnl = close_qty * unit_pnl
                s['realized'] += pnl
                s['pos'] = max(0.0, pos - size)
                if s['pos'] <= 1e-12:
                    s['avg_cost'] = 0.0
                if close_qty > 0:
                    if unit_pnl > 1e-12:
                        s['wins'] += 1
                    elif unit_pnl < -1e-12:
                        s['losses'] += 1

            equity = s['realized']
            if equity > s['peak']:
                s['peak'] = equity
            peak = s['peak']
            if peak > 1e-12:
                dd = (peak - equity) / peak * 100.0
                if dd > s['max_dd']:
                    s['max_dd'] = dd

        rows: list[PerfRow] = []
        for sid, s in state.items():
            closed = s['wins'] + s['losses']
            win_rate = (s['wins'] / closed) if closed > 0 else 0.0
            rows.append(
                PerfRow(
                    strategy_id=sid,
                    trades=int(s['trades']),
                    turnover=float(s['turnover']),
                    realized_pnl=float(s['realized']),
                    max_drawdown_pct=float(s['max_dd']),
                    win_rate=float(win_rate),
                    wins=int(s['wins']),
                    losses=int(s['losses']),
                    net_position=float(s['pos']),
                    avg_cost=float(s['avg_cost']),
                    last_time_utc=str(s['last_time_utc']),
                )
            )

        rows.sort(key=lambda x: x.realized_pnl, reverse=True)
        return rows


def filter_promotion_candidates(
    rows: list[PerfRow],
    min_pnl: float,
    max_dd_pct: float,
    min_trades: int,
    min_win_rate: float,
) -> list[PerfRow]:
    out: list[PerfRow] = []
    for r in rows:
        if r.realized_pnl < min_pnl:
            continue
        if r.max_drawdown_pct > max_dd_pct:
            continue
        if r.trades < min_trades:
            continue
        if r.win_rate < min_win_rate:
            continue
        out.append(r)
    return out


def save_promotion_candidate(
    path: Path,
    row: PerfRow,
    thresholds: dict[str, Any],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'status': 'LIVE_LOG_APPROVED',
        'approved_at_utc': datetime.now(timezone.utc).isoformat(),
        'strategy_id': row.strategy_id,
        'metrics': {
            'trades': row.trades,
            'turnover': row.turnover,
            'realized_pnl': row.realized_pnl,
            'max_drawdown_pct': row.max_drawdown_pct,
            'win_rate': row.win_rate,
            'wins': row.wins,
            'losses': row.losses,
            'net_position': row.net_position,
            'avg_cost': row.avg_cost,
            'last_time_utc': row.last_time_utc,
        },
        'thresholds': thresholds,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
