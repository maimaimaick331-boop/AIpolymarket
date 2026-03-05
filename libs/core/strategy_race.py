from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json
import random
from urllib.request import Request, urlopen

from libs.core.paper_sim import PaperSimulator, SimulationConfig, load_snapshots


@dataclass
class StrategySpec:
    strategy_id: str
    name: str
    strategy_type: str
    params: dict[str, Any]
    source: str


@dataclass
class StrategyRunResult:
    strategy: dict[str, Any]
    metrics: dict[str, Any]
    recent_fills: list[dict[str, Any]]
    summary_file: str
    fills_file: str


@dataclass
class RaceResult:
    started_at_utc: str
    finished_at_utc: str
    snapshot_count: int
    token_universe: list[str]
    candidates: int
    leaderboard: list[dict[str, Any]]
    runs: list[StrategyRunResult]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _try_openclaw_generate(
    endpoint: str,
    n: int,
    latest_snapshot: dict[str, Any],
    timeout_sec: float,
) -> list[StrategySpec]:
    payload = {
        'task': 'generate_polymarket_strategies',
        'count': n,
        'constraints': {
            'types': ['periodic', 'mean_reversion'],
            'risk_loss_limit_pct_range': [0.5, 5.0],
            'order_qty_range': [2, 30],
            'hold_ticks_range': [1, 6],
            'mean_rev_window_range': [4, 20],
            'mean_rev_threshold_range': [0.005, 0.04],
        },
        'market_context': {
            'markets_count': latest_snapshot.get('markets_count', 0),
            'books_count': latest_snapshot.get('books_count', 0),
        },
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

    req = Request(
        endpoint,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
        method='POST',
    )
    with urlopen(req, timeout=timeout_sec) as resp:
        data = json.loads(resp.read().decode('utf-8'))

    raw = data.get('strategies', []) if isinstance(data, dict) else []
    out: list[StrategySpec] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        st = str(item.get('strategy_type', '')).strip().lower()
        if st not in {'periodic', 'mean_reversion'}:
            continue
        params = item.get('params', {})
        if not isinstance(params, dict):
            params = {}
        out.append(
            StrategySpec(
                strategy_id=f'ai-{i+1:03d}',
                name=str(item.get('name') or f'AI-{st}-{i+1}'),
                strategy_type=st,
                params=params,
                source='openclaw',
            )
        )
    return out


def generate_strategy_candidates(
    count: int,
    seed: int,
    openclaw_endpoint: str,
    openclaw_timeout_sec: float,
    latest_snapshot: dict[str, Any],
) -> list[StrategySpec]:
    candidates: list[StrategySpec] = []

    if openclaw_endpoint:
        try:
            candidates = _try_openclaw_generate(
                endpoint=openclaw_endpoint,
                n=count,
                latest_snapshot=latest_snapshot,
                timeout_sec=openclaw_timeout_sec,
            )
        except Exception:
            candidates = []

    if candidates:
        return candidates[:count]

    rng = random.Random(seed)
    out: list[StrategySpec] = []
    for i in range(count):
        if i % 2 == 0:
            out.append(
                StrategySpec(
                    strategy_id=f'gen-{i+1:03d}',
                    name=f'Periodic-{i+1}',
                    strategy_type='periodic',
                    params={
                        'order_qty': round(rng.uniform(4, 22), 2),
                        'hold_ticks': rng.randint(1, 5),
                        'risk_loss_limit_pct': round(rng.uniform(1.0, 4.0), 2),
                    },
                    source='template',
                )
            )
        else:
            out.append(
                StrategySpec(
                    strategy_id=f'gen-{i+1:03d}',
                    name=f'MeanRev-{i+1}',
                    strategy_type='mean_reversion',
                    params={
                        'order_qty': round(rng.uniform(3, 18), 2),
                        'hold_ticks': rng.randint(1, 4),
                        'risk_loss_limit_pct': round(rng.uniform(0.8, 3.5), 2),
                        'mean_rev_window': rng.randint(4, 14),
                        'mean_rev_threshold': round(rng.uniform(0.008, 0.035), 4),
                    },
                    source='template',
                )
            )
    return out


def _resolve_token_universe(snapshots: list[tuple[str, dict[str, Any]]], token_limit: int) -> list[str]:
    for _, payload in reversed(snapshots):
        books = payload.get('books', {})
        if isinstance(books, dict) and books:
            return list(books.keys())[: max(1, token_limit)]
    return []


def _score(metrics: dict[str, Any]) -> float:
    pnl = float(metrics.get('realized_pnl', 0.0))
    dd = float(metrics.get('max_drawdown_pct', 0.0))
    fees = float(metrics.get('total_fees', 0.0))
    trades = float(metrics.get('fills_count', 0.0))
    low_trade_penalty = 8.0 if trades < 5 else 0.0
    risk_penalty = 1000.0 if bool(metrics.get('risk_halted')) else 0.0
    return pnl - 0.15 * dd - 0.03 * fees - 0.001 * trades - low_trade_penalty - risk_penalty


def run_strategy_race(
    snapshot_paths: list[Path],
    candidates: list[StrategySpec],
    token_limit: int,
    initial_cash: float,
    fee_bps: float,
    output_dir: Path,
) -> RaceResult:
    started = _now_iso()
    snapshots = load_snapshots(snapshot_paths)
    token_universe = _resolve_token_universe(snapshots, token_limit=token_limit)

    runs: list[StrategyRunResult] = []
    leaderboard: list[dict[str, Any]] = []

    for spec in candidates:
        params = spec.params
        config = SimulationConfig(
            initial_cash=initial_cash,
            fee_bps=fee_bps,
            strategy=spec.strategy_type,
            risk_loss_limit_pct=float(params.get('risk_loss_limit_pct', 3.0)),
            mean_rev_window=int(params.get('mean_rev_window', 8)),
            mean_rev_threshold=float(params.get('mean_rev_threshold', 0.015)),
        )
        sim = PaperSimulator(config)
        result = sim.run(
            snapshots=snapshots,
            token_universe=token_universe,
            order_qty=float(params.get('order_qty', 10.0)),
            hold_ticks=max(1, int(params.get('hold_ticks', 1))),
        )

        result_dict = asdict(result)
        metrics = {
            'realized_pnl': result_dict['realized_pnl'],
            'final_equity': result_dict['final_equity'],
            'max_drawdown_pct': result_dict['max_drawdown_pct'],
            'win_rate': result_dict['win_rate'],
            'fills_count': result_dict['fills_count'],
            'total_fees': result_dict['total_fees'],
            'risk_halted': result_dict['risk_halted'],
            'strategy': result_dict['strategy'],
        }
        metrics['score'] = _score(metrics)

        sid = spec.strategy_id
        summary_file = output_dir / f'strategy_{sid}_summary.json'
        fills_file = output_dir / f'strategy_{sid}_fills.jsonl'
        summary_file.write_text(json.dumps(result_dict, ensure_ascii=False, indent=2), encoding='utf-8')
        with fills_file.open('w', encoding='utf-8') as fp:
            for fill in result_dict.get('fills', []):
                fp.write(json.dumps(fill, ensure_ascii=False))
                fp.write('\n')

        recent = result_dict.get('fills', [])[-20:]
        run = StrategyRunResult(
            strategy=asdict(spec),
            metrics=metrics,
            recent_fills=recent,
            summary_file=str(summary_file),
            fills_file=str(fills_file),
        )
        runs.append(run)

        leaderboard.append(
            {
                'strategy_id': sid,
                'name': spec.name,
                'type': spec.strategy_type,
                'source': spec.source,
                'score': metrics['score'],
                'pnl': metrics['realized_pnl'],
                'max_drawdown_pct': metrics['max_drawdown_pct'],
                'win_rate': metrics['win_rate'],
                'fills_count': metrics['fills_count'],
                'risk_halted': metrics['risk_halted'],
            }
        )

    leaderboard.sort(key=lambda x: x['score'], reverse=True)

    return RaceResult(
        started_at_utc=started,
        finished_at_utc=_now_iso(),
        snapshot_count=len(snapshots),
        token_universe=token_universe,
        candidates=len(candidates),
        leaderboard=leaderboard,
        runs=runs,
    )
