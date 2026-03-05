from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path
import sys

if __package__ in (None, ''):
    # Allow running as: python apps/trader/run_paper_sim.py
    sys.path.append(str(Path(__file__).resolve().parents[2]))

from libs.core.config import load_settings
from libs.core.paper_sim import PaperSimulator, SimulationConfig, load_snapshots
from libs.core.storage import append_jsonl, utc_now_slug, write_json


def _print_report(result: dict) -> None:
    print('\n=== 模拟盘回放报告 ===')
    print(f"策略模式: {result.get('strategy', 'periodic')}")
    print(f"快照数量: {result['snapshot_count']}")
    print(f"Token 数量: {len(result['token_universe'])}")
    print(f"成交笔数: {result['fills_count']}")
    print(f"胜率(卖出): {result.get('win_rate', 0.0) * 100:.2f}%")
    print(f"最大回撤: {result.get('max_drawdown_pct', 0.0):.3f}%")
    print(f"总手续费: {result.get('total_fees', 0.0):.6f}")
    print(f"总换手: {result.get('turnover', 0.0):.4f}")
    print(f"期末现金: {result['final_cash']:.4f}")
    print(f"期末权益: {result['final_equity']:.4f}")
    print(f"收益(PnL): {result['realized_pnl']:.4f}")
    print(f"未平仓数量: {len(result['open_positions'])}")
    print(f"风险熔断: {'是' if result.get('risk_halted') else '否'}")

    events = result.get('risk_events', [])
    if events:
        print('\n风险事件:')
        for event in events:
            print(f'- {event}')

    fills = result.get('fills', [])
    if fills:
        print('\n最近成交（最多 5 条）:')
        for fill in fills[-5:]:
            side = '买入' if str(fill['side']).lower() == 'buy' else '卖出'
            print(
                f"- t{fill['tick']} {side} token={fill['token_id'][:10]}... "
                f"数量={fill['quantity']:.4f} 价格={fill['price']:.4f} 手续费={fill['fee']:.6f}"
            )


def parse_args() -> argparse.Namespace:
    settings = load_settings()
    parser = argparse.ArgumentParser(description='基于 Polymarket 快照执行模拟盘回放。')
    parser.add_argument('--snapshot-glob', default='data/raw/polymarket/snapshots/*.json', help='快照文件匹配模式。')
    parser.add_argument('--max-snapshots', type=int, default=200, help='最多加载多少个快照。')
    parser.add_argument('--token-limit', type=int, default=3, help='参与回放的 token 数量。')
    parser.add_argument('--initial-cash', type=float, default=settings.paper_initial_cash, help='初始资金。')
    parser.add_argument('--fee-bps', type=float, default=settings.paper_fee_bps, help='手续费（基点 bps）。')
    parser.add_argument('--order-qty', type=float, default=settings.paper_order_qty, help='每次下单数量。')
    parser.add_argument('--hold-ticks', type=int, default=settings.paper_hold_ticks, help='买入后持有多少 tick 再卖出。')
    parser.add_argument('--strategy', choices=['periodic', 'mean_reversion'], default=settings.paper_strategy, help='策略模式。')
    parser.add_argument('--risk-loss-limit-pct', type=float, default=settings.paper_risk_loss_limit_pct, help='亏损熔断阈值(%)。')
    parser.add_argument('--output-dir', default=str(settings.paper_dir), help='结果输出目录。')
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    snapshot_paths = sorted(Path().glob(args.snapshot_glob))
    if args.max_snapshots > 0:
        snapshot_paths = snapshot_paths[-args.max_snapshots :]

    snapshots = load_snapshots(snapshot_paths)
    if not snapshots:
        print('未加载到快照，请先运行抓取程序。')
        return 1

    token_universe: list[str] = []
    for _, payload in snapshots:
        books = payload.get('books', {})
        if not isinstance(books, dict) or not books:
            continue
        token_universe = list(books.keys())[: max(1, args.token_limit)]
        break
    if not token_universe:
        print('快照中未找到可用的订单簿数据。')
        return 1

    config = SimulationConfig(
        initial_cash=args.initial_cash,
        fee_bps=args.fee_bps,
        strategy=args.strategy,
        risk_loss_limit_pct=max(0.1, args.risk_loss_limit_pct),
    )
    sim = PaperSimulator(config)
    result = sim.run(
        snapshots=snapshots,
        token_universe=token_universe,
        order_qty=args.order_qty,
        hold_ticks=max(1, args.hold_ticks),
    )

    output_dir = Path(args.output_dir)
    slug = utc_now_slug()
    summary_path = output_dir / f'paper_summary_{slug}.json'
    fills_path = output_dir / f'paper_fills_{slug}.jsonl'

    result_dict = asdict(result)
    write_json(summary_path, result_dict)
    for fill in result.fills:
        append_jsonl(fills_path, fill)

    _print_report(result_dict)
    print(f'汇总文件={summary_path}')
    print(f'成交文件={fills_path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
