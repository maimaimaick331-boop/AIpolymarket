from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import sys
import time

if __package__ in (None, ''):
    # Allow running as: python apps/trader/run_autopilot.py
    sys.path.append(str(Path(__file__).resolve().parents[2]))

from apps.trader.run_fetcher import fetch_once
from apps.trader.run_paper_viz import _html  # local project internal use
from libs.connectors.polymarket import PolymarketPublicClient
from libs.core.config import load_settings
from libs.core.paper_sim import PaperSimulator, SimulationConfig, load_snapshots
from libs.core.storage import append_jsonl, utc_now_slug, write_json


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_args() -> argparse.Namespace:
    settings = load_settings()
    parser = argparse.ArgumentParser(description='自动循环执行：抓取行情 -> 模拟盘回放 -> 更新可视化报告。')
    parser.add_argument('--interval-sec', type=int, default=settings.realtime_interval_sec, help='循环间隔秒数。')
    parser.add_argument('--market-limit', type=int, default=settings.market_limit, help='每轮抓取市场数。')
    parser.add_argument('--max-books', type=int, default=settings.max_books, help='每轮抓取订单簿数。')
    parser.add_argument('--max-snapshots', type=int, default=300, help='回放时最多读取多少个快照。')
    parser.add_argument('--token-limit', type=int, default=5, help='回放时选择多少个 token。')
    parser.add_argument('--strategy', choices=['periodic', 'mean_reversion'], default=settings.paper_strategy, help='模拟策略。')
    parser.add_argument('--order-qty', type=float, default=settings.paper_order_qty, help='每次下单数量。')
    parser.add_argument('--hold-ticks', type=int, default=settings.paper_hold_ticks, help='持仓 tick。')
    parser.add_argument('--risk-loss-limit-pct', type=float, default=settings.paper_risk_loss_limit_pct, help='亏损熔断阈值。')
    parser.add_argument('--refresh-sec', type=int, default=settings.dashboard_refresh_sec, help='网页自动刷新秒数。')
    parser.add_argument('--once', action='store_true', help='只跑一轮。')
    return parser.parse_args()


def _select_token_universe(snapshots: list[tuple[str, dict]], token_limit: int) -> list[str]:
    token_universe: list[str] = []
    for _, payload in reversed(snapshots):
        books = payload.get('books', {})
        if not isinstance(books, dict) or not books:
            continue
        token_universe = list(books.keys())[: max(1, token_limit)]
        break
    return token_universe


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
    paper_dir = settings.paper_dir
    report_path = paper_dir / 'report_latest.html'

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
        snapshots = load_snapshots(snapshot_paths)
        token_universe = _select_token_universe(snapshots, token_limit=args.token_limit)

        if token_universe:
            sim_config = SimulationConfig(
                initial_cash=settings.paper_initial_cash,
                fee_bps=settings.paper_fee_bps,
                strategy=args.strategy,
                risk_loss_limit_pct=max(0.1, args.risk_loss_limit_pct),
            )
            sim = PaperSimulator(sim_config)
            result = sim.run(
                snapshots=snapshots,
                token_universe=token_universe,
                order_qty=args.order_qty,
                hold_ticks=max(1, args.hold_ticks),
            )

            summary_path = paper_dir / f'paper_summary_{slug}.json'
            fills_path = paper_dir / f'paper_fills_{slug}.jsonl'
            result_dict = asdict(result)
            write_json(summary_path, result_dict)
            for fill in result.fills:
                append_jsonl(fills_path, fill)

            report_html = _html(result_dict, result_dict.get('fills', []), summary_path, fills_path, auto_refresh_sec=max(0, args.refresh_sec))
            write_json(paper_dir / 'latest_state.json', {
                'updated_at_utc': _now_iso(),
                'summary': str(summary_path),
                'fills': str(fills_path),
                'report': str(report_path),
            })
            report_path.write_text(report_html, encoding='utf-8')

            print(
                f"[{_now_iso()}] snap={snapshot['books_count']}books "
                f"fills={result.fills_count} equity={result.final_equity:.4f} pnl={result.realized_pnl:.4f} "
                f"strategy={result.strategy} risk_halted={result.risk_halted}"
            )
        else:
            print(f"[{_now_iso()}] 快照已写入，但当前没有可回放 token。")

        if args.once:
            return 0
        time.sleep(max(1, args.interval_sec))


if __name__ == '__main__':
    raise SystemExit(main())
