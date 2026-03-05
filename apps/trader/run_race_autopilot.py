from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

if __package__ in (None, ''):
    # Allow running as: python apps/trader/run_race_autopilot.py
    sys.path.append(str(Path(__file__).resolve().parents[2]))

from apps.trader.run_fetcher import fetch_once
from libs.connectors.polymarket import PolymarketPublicClient
from libs.core.config import load_settings
from libs.core.storage import append_jsonl, utc_now_slug, write_json
from libs.core.strategy_race import generate_strategy_candidates, run_strategy_race


def parse_args() -> argparse.Namespace:
    settings = load_settings()
    parser = argparse.ArgumentParser(description='自动循环执行 AI 策略赛马（抓取 -> 赛马 -> 更新看板数据）。')
    parser.add_argument('--interval-sec', type=int, default=settings.realtime_interval_sec, help='循环间隔秒数。')
    parser.add_argument('--market-limit', type=int, default=settings.market_limit, help='每轮抓取市场数。')
    parser.add_argument('--max-books', type=int, default=settings.max_books, help='每轮抓取订单簿数。')
    parser.add_argument('--max-snapshots', type=int, default=300, help='赛马回放最大快照数。')
    parser.add_argument('--token-limit', type=int, default=6, help='赛马 token 数。')
    parser.add_argument('--candidates', type=int, default=10, help='每轮候选策略数。')
    parser.add_argument('--seed', type=int, default=20260304, help='模板策略随机种子。')
    parser.add_argument('--openclaw-endpoint', default='', help='OpenClaw 生成策略接口。')
    parser.add_argument('--openclaw-timeout-sec', type=float, default=20.0, help='OpenClaw 请求超时。')
    parser.add_argument('--once', action='store_true', help='只跑一轮。')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = load_settings()

    client = PolymarketPublicClient(
        gamma_base_url=settings.gamma_base_url,
        clob_base_url=settings.clob_base_url,
        timeout_sec=settings.timeout_sec,
    )

    snapshots_dir = settings.output_dir / 'snapshots'
    fetch_log = settings.output_dir / 'fetch_log.jsonl'
    race_dir = settings.paper_dir / 'race'
    race_dir.mkdir(parents=True, exist_ok=True)

    while True:
        snapshot = fetch_once(client, market_limit=args.market_limit, max_books=args.max_books)
        slug = utc_now_slug()
        snapshot_path = snapshots_dir / f'{slug}.json'
        write_json(snapshot_path, snapshot)
        append_jsonl(
            fetch_log,
            {
                'fetched_at_utc': snapshot['fetched_at_utc'],
                'snapshot_file': str(snapshot_path),
                'markets_count': snapshot['markets_count'],
                'books_count': snapshot['books_count'],
            },
        )

        snapshot_paths = sorted(snapshots_dir.glob('*.json'))[-args.max_snapshots :]
        latest = {}
        try:
            latest = json.loads(snapshot_path.read_text(encoding='utf-8'))
        except Exception:
            latest = {}

        cands = generate_strategy_candidates(
            count=max(2, args.candidates),
            seed=args.seed,
            openclaw_endpoint=args.openclaw_endpoint,
            openclaw_timeout_sec=args.openclaw_timeout_sec,
            latest_snapshot=latest,
        )

        race = run_strategy_race(
            snapshot_paths=snapshot_paths,
            candidates=cands,
            token_limit=max(1, args.token_limit),
            initial_cash=settings.paper_initial_cash,
            fee_bps=settings.paper_fee_bps,
            output_dir=race_dir,
        )

        race_dict = {
            'started_at_utc': race.started_at_utc,
            'finished_at_utc': race.finished_at_utc,
            'snapshot_count': race.snapshot_count,
            'token_universe': race.token_universe,
            'candidates': race.candidates,
            'leaderboard': race.leaderboard,
            'runs': [
                {
                    'strategy': r.strategy,
                    'metrics': r.metrics,
                    'recent_fills': r.recent_fills,
                    'summary_file': r.summary_file,
                    'fills_file': r.fills_file,
                }
                for r in race.runs
            ],
        }

        write_json(race_dir / f'race_summary_{slug}.json', race_dict)
        write_json(race_dir / 'race_latest.json', race_dict)

        if race.leaderboard:
            top = race.leaderboard[0]
            print(
                f"[{snapshot['fetched_at_utc']}] candidates={race.candidates} top={top['strategy_id']} "
                f"score={top['score']:.4f} pnl={top['pnl']:.4f} dd={top['max_drawdown_pct']:.3f}%"
            )
        else:
            print(f"[{snapshot['fetched_at_utc']}] 本轮无可用策略结果")

        if args.once:
            return 0
        time.sleep(max(1, args.interval_sec))


if __name__ == '__main__':
    raise SystemExit(main())
