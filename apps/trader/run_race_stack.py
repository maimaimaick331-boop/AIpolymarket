from __future__ import annotations

import argparse
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

if __package__ in (None, ''):
    # Allow running as: python apps/trader/run_race_stack.py
    sys.path.append(str(Path(__file__).resolve().parents[2]))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='一键启动：AI赛马自动循环 + 赛马看板服务。')
    parser.add_argument('--port', type=int, default=8770, help='赛马看板端口。')
    parser.add_argument('--interval-sec', type=int, default=20, help='赛马循环间隔秒数。')
    parser.add_argument('--candidates', type=int, default=10, help='每轮策略候选数。')
    parser.add_argument('--token-limit', type=int, default=6, help='赛马 token 数。')
    parser.add_argument('--max-snapshots', type=int, default=300, help='赛马回放最大快照数。')
    parser.add_argument('--openclaw-endpoint', default='', help='OpenClaw 生成策略接口。')
    parser.add_argument('--auto-refresh-sec', type=int, default=5, help='看板自动刷新秒数。')
    return parser.parse_args()


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=4)
    except subprocess.TimeoutExpired:
        proc.kill()


def main() -> int:
    args = parse_args()

    autopilot_cmd = [
        sys.executable,
        'apps/trader/run_race_autopilot.py',
        '--interval-sec',
        str(args.interval_sec),
        '--candidates',
        str(args.candidates),
        '--token-limit',
        str(args.token_limit),
        '--max-snapshots',
        str(args.max_snapshots),
    ]
    if args.openclaw_endpoint:
        autopilot_cmd.extend(['--openclaw-endpoint', args.openclaw_endpoint])

    viz_cmd = [
        sys.executable,
        'apps/trader/run_race_viz.py',
        '--race-summary',
        'data/raw/polymarket/paper/race/race_latest.json',
        '--serve',
        '--port',
        str(args.port),
        '--auto-refresh-sec',
        str(args.auto_refresh_sec),
    ]

    print('启动赛马自动循环:')
    print('  ' + shlex.join(autopilot_cmd))
    autopilot_proc = subprocess.Popen(autopilot_cmd)

    # Give autopilot a moment to create/refresh race_latest.json
    time.sleep(1)

    print('启动赛马看板服务:')
    print('  ' + shlex.join(viz_cmd))
    print(f'访问地址: http://127.0.0.1:{args.port}/race_report_latest.html')
    viz_proc = subprocess.Popen(viz_cmd)

    def _handle_stop(signum, frame):  # noqa: ANN001
        _terminate(viz_proc)
        _terminate(autopilot_proc)
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    try:
        while True:
            if autopilot_proc.poll() is not None:
                print('赛马自动循环已退出，正在停止看板服务。')
                _terminate(viz_proc)
                return autopilot_proc.returncode or 1
            if viz_proc.poll() is not None:
                print('看板服务已退出，正在停止赛马自动循环。')
                _terminate(autopilot_proc)
                return viz_proc.returncode or 1
            time.sleep(0.8)
    finally:
        _terminate(viz_proc)
        _terminate(autopilot_proc)


if __name__ == '__main__':
    raise SystemExit(main())
