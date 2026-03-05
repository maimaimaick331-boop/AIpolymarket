from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

if __package__ in (None, ''):
    # Allow running as: python apps/trader/run_strategy_gate.py
    sys.path.append(str(Path(__file__).resolve().parents[2]))

from libs.core.storage import write_json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='策略晋级网关：从赛马结果筛选可候选实盘的策略。')
    parser.add_argument('--race-latest', default='data/raw/polymarket/paper/race/race_latest.json', help='赛马汇总文件。')
    parser.add_argument('--strategy-id', default='', help='手动指定策略ID，不填则自动选 Top1 合格策略。')
    parser.add_argument('--min-pnl', type=float, default=0.0, help='最小 PnL 阈值。')
    parser.add_argument('--max-dd-pct', type=float, default=1.5, help='最大回撤阈值(%)。')
    parser.add_argument('--min-fills', type=int, default=10, help='最小成交笔数。')
    parser.add_argument('--min-win-rate', type=float, default=0.45, help='最小胜率(0-1)。')
    parser.add_argument('--output', default='data/raw/polymarket/paper/deploy/promotion_candidate.json', help='输出候选配置文件。')
    return parser.parse_args()


def _is_qualified(row: dict, args: argparse.Namespace) -> bool:
    if bool(row.get('risk_halted')):
        return False
    if float(row.get('pnl', 0.0)) < args.min_pnl:
        return False
    if float(row.get('max_drawdown_pct', 999.0)) > args.max_dd_pct:
        return False
    if int(row.get('fills_count', 0)) < args.min_fills:
        return False
    if float(row.get('win_rate', 0.0)) < args.min_win_rate:
        return False
    return True


def main() -> int:
    args = parse_args()
    race_path = Path(args.race_latest)
    if not race_path.exists():
        print(f'未找到赛马文件: {race_path}')
        return 1

    race = json.loads(race_path.read_text(encoding='utf-8'))
    leaderboard = race.get('leaderboard', [])
    runs = race.get('runs', [])
    if not isinstance(leaderboard, list) or not leaderboard:
        print('赛马排行榜为空，无法晋级。')
        return 1

    selected = None
    if args.strategy_id:
        for row in leaderboard:
            if str(row.get('strategy_id')) == args.strategy_id:
                selected = row
                break
    else:
        for row in leaderboard:
            if _is_qualified(row, args):
                selected = row
                break

    if selected is None:
        print('没有策略满足晋级门槛，继续赛马优化。')
        return 1

    strategy_id = str(selected.get('strategy_id'))
    run_detail = None
    for item in runs:
        strategy = item.get('strategy', {}) if isinstance(item, dict) else {}
        if str(strategy.get('strategy_id')) == strategy_id:
            run_detail = item
            break

    if run_detail is None:
        print(f'未找到策略 {strategy_id} 的详细记录。')
        return 1

    candidate = {
        'status': 'PAPER_APPROVED',
        'approved_from': str(race_path),
        'strategy': run_detail.get('strategy', {}),
        'metrics': run_detail.get('metrics', {}),
        'checks': {
            'min_pnl': args.min_pnl,
            'max_dd_pct': args.max_dd_pct,
            'min_fills': args.min_fills,
            'min_win_rate': args.min_win_rate,
        },
        'next_actions': [
            '启用小资金实盘灰度（建议 1%-3% 资金）',
            '启用实时风控：日损熔断/单市场限额/异常行情禁开仓',
            '保持可回滚：任何异常立即切回模拟盘',
        ],
    }

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_json(out, candidate)

    print(f"晋级策略: {strategy_id}")
    print(f"输出文件: {out}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
