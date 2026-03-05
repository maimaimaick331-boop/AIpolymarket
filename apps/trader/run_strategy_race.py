from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path
import sys

if __package__ in (None, ''):
    # Allow running as: python apps/trader/run_strategy_race.py
    sys.path.append(str(Path(__file__).resolve().parents[2]))

from libs.core.config import load_settings
from libs.core.storage import utc_now_slug, write_json
from libs.core.strategy_race import (
    generate_strategy_candidates,
    run_strategy_race,
)


def parse_args() -> argparse.Namespace:
    settings = load_settings()
    parser = argparse.ArgumentParser(description='执行 AI/模板策略赛马：生成多策略并回放评分。')
    parser.add_argument('--snapshot-glob', default='data/raw/polymarket/snapshots/*.json', help='快照文件匹配模式。')
    parser.add_argument('--max-snapshots', type=int, default=300, help='最多读取多少个快照。')
    parser.add_argument('--token-limit', type=int, default=5, help='回放使用多少个 token。')
    parser.add_argument('--candidates', type=int, default=8, help='策略候选数量。')
    parser.add_argument('--seed', type=int, default=20260304, help='模板策略随机种子。')
    parser.add_argument('--openclaw-endpoint', default='', help='OpenClaw 生成策略接口（可选）。')
    parser.add_argument('--openclaw-timeout-sec', type=float, default=20.0, help='OpenClaw 调用超时。')
    parser.add_argument('--initial-cash', type=float, default=settings.paper_initial_cash, help='初始资金。')
    parser.add_argument('--fee-bps', type=float, default=settings.paper_fee_bps, help='手续费 bps。')
    parser.add_argument('--output-dir', default=str(settings.paper_dir / 'race'), help='输出目录。')
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    snapshot_paths = sorted(Path().glob(args.snapshot_glob))
    if args.max_snapshots > 0:
        snapshot_paths = snapshot_paths[-args.max_snapshots :]
    if not snapshot_paths:
        print('未找到快照，请先运行抓取程序。')
        return 1

    latest_snapshot = {}
    try:
        import json

        latest_snapshot = json.loads(snapshot_paths[-1].read_text(encoding='utf-8'))
    except Exception:
        latest_snapshot = {}

    candidates = generate_strategy_candidates(
        count=max(2, args.candidates),
        seed=args.seed,
        openclaw_endpoint=args.openclaw_endpoint,
        openclaw_timeout_sec=args.openclaw_timeout_sec,
        latest_snapshot=latest_snapshot,
    )

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    race_result = run_strategy_race(
        snapshot_paths=snapshot_paths,
        candidates=candidates,
        token_limit=max(1, args.token_limit),
        initial_cash=args.initial_cash,
        fee_bps=args.fee_bps,
        output_dir=output_dir,
    )

    slug = utc_now_slug()
    race_summary_path = output_dir / f'race_summary_{slug}.json'
    latest_path = output_dir / 'race_latest.json'

    result_dict = asdict(race_result)
    write_json(race_summary_path, result_dict)
    write_json(latest_path, result_dict)

    print(f'候选策略数={race_result.candidates} 快照数={race_result.snapshot_count} token数={len(race_result.token_universe)}')
    if race_result.leaderboard:
        top = race_result.leaderboard[0]
        print(
            f"Top1={top['strategy_id']} {top['name']} type={top['type']} "
            f"score={top['score']:.4f} pnl={top['pnl']:.4f} dd={top['max_drawdown_pct']:.3f}%"
        )

    print(f'赛马汇总={race_summary_path}')
    print(f'赛马最新={latest_path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
