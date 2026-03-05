from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
import threading
import time

from libs.quant.db import QuantDB
from libs.quant.signal_engine import QuantSignal


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


@dataclass
class OrchestratorConfig:
    mode: str = 'paper'  # paper | live
    cycle_sec: int = 12
    market_limit: int = 120
    max_books: int = 400
    max_signals_per_cycle: int = 16
    provider_id: str = ''
    ai_prompt: str = ''
    enable_arb: bool = True
    enable_mm: bool = True
    enable_ai: bool = True
    dry_run: bool = False
    enforce_live_gate: bool = True
    live_gate_min_hours: int = 72
    live_gate_min_win_rate: float = 0.45
    live_gate_min_pnl: float = 0.0
    live_gate_min_fills: int = 20


class PolymarketQuantOrchestrator:
    def __init__(
        self,
        *,
        db: QuantDB,
        market_data_engine: Any,
        signal_engine: Any,
        risk_engine: Any,
        execution_engine: Any,
    ) -> None:
        self.db = db
        self.market_data_engine = market_data_engine
        self.signal_engine = signal_engine
        self.risk_engine = risk_engine
        self.execution_engine = execution_engine
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._cfg = OrchestratorConfig()
        self._status: dict[str, Any] = {
            'running': False,
            'started_at_utc': '',
            'updated_at_utc': '',
            'cycle': 0,
            'phase': 'idle',
            'last_error': '',
            'last_summary': {},
        }

    def _set_status(self, **kwargs: Any) -> None:
        with self._lock:
            self._status.update(kwargs)
            self._status['updated_at_utc'] = _now_utc()

    def status(self) -> dict[str, Any]:
        with self._lock:
            out = dict(self._status)
            cfg = asdict(self._cfg)
        out['config'] = cfg
        out['db'] = self.db.summary()
        out['market_data'] = self.market_data_engine.state()
        out['risk'] = self.risk_engine.snapshot()
        return out

    def _cycle_once(self, cfg: OrchestratorConfig) -> dict[str, Any]:
        self._set_status(phase='market_refresh', last_error='')
        market_refresh = self.market_data_engine.refresh(market_limit=cfg.market_limit, max_books=cfg.max_books)
        markets = self.db.list_markets(limit=max(20, min(cfg.market_limit * 4, 2000)))
        books = {str(x.get('token_id', '')): x for x in self.db.list_books(limit=max(100, cfg.max_books * 2))}

        self._set_status(phase='risk_refresh')
        risk_snapshot = self.risk_engine.refresh_from_paper()
        total_exposure = float(risk_snapshot.get('total_exposure', 0.0))
        account_daily_pnl = float((risk_snapshot.get('account_risk', {}) or {}).get('daily_pnl', 0.0))

        self._set_status(phase='signal_generate')
        signals: list[QuantSignal] = self.signal_engine.generate(
            markets=markets,
            books=books,
            provider_id=cfg.provider_id,
            ai_prompt=cfg.ai_prompt,
            enable_arb=cfg.enable_arb,
            enable_mm=cfg.enable_mm,
            enable_ai=cfg.enable_ai,
        )
        signals.sort(key=lambda x: float(x.score), reverse=True)
        dropped_no_book = 0
        ready_signals: list[QuantSignal] = []
        for sig in signals:
            if self.market_data_engine.get_book(sig.token_id) is None:
                dropped_no_book += 1
                continue
            ready_signals.append(sig)
        signals = ready_signals
        signals = signals[: max(1, min(cfg.max_signals_per_cycle, 200))]

        self._set_status(phase='execution')
        live_gate_map: dict[str, dict[str, Any]] = {}
        if cfg.mode == 'live' and cfg.enforce_live_gate:
            gate = self.db.live_gate_status(
                min_hours=cfg.live_gate_min_hours,
                min_win_rate=cfg.live_gate_min_win_rate,
                min_pnl=cfg.live_gate_min_pnl,
                min_fills=cfg.live_gate_min_fills,
                strategy_ids=sorted({x.strategy_id for x in signals}),
            )
            live_gate_map = {str(x.get('strategy_id', '')): x for x in gate.get('rows', []) if isinstance(x, dict)}

        created = 0
        executed = 0
        blocked = 0
        failed = 0
        for sig in signals:
            if self._stop.is_set():
                break
            row = sig.to_row()
            signal_id = self.db.insert_signal(row)
            created += 1
            if cfg.mode == 'paper':
                race_allow, race_reason = self.risk_engine.paper_strategy_gate(sig.strategy_id)
                if not race_allow:
                    self.db.update_signal_status(signal_id, 'blocked', race_reason)
                    self.db.insert_event(
                        'signal_blocked_race',
                        f"策略 {sig.strategy_id} 被赛马淘汰门禁阻断",
                        {
                            'signal_id': signal_id,
                            'strategy_id': sig.strategy_id,
                            'market_id': sig.market_id,
                            'token_id': sig.token_id,
                            'reason': race_reason,
                        },
                    )
                    blocked += 1
                    continue
            if cfg.mode == 'live' and cfg.enforce_live_gate:
                gate_row = live_gate_map.get(sig.strategy_id, {})
                if not bool(gate_row.get('eligible', False)):
                    reason = 'live_gate_not_eligible'
                    reasons = gate_row.get('reasons', [])
                    if isinstance(reasons, list) and reasons:
                        reason = f"live_gate_not_eligible:{'|'.join([str(x) for x in reasons[:4]])}"
                    self.db.update_signal_status(signal_id, 'blocked', reason)
                    self.db.insert_event(
                        'signal_blocked_live_gate',
                        f"策略 {sig.strategy_id} 未通过72h实盘门禁",
                        {
                            'signal_id': signal_id,
                            'strategy_id': sig.strategy_id,
                            'market_id': sig.market_id,
                            'token_id': sig.token_id,
                            'reason': reason,
                            'gate': gate_row,
                        },
                    )
                    blocked += 1
                    continue
            decision = self.risk_engine.evaluate_signal(row, total_exposure=total_exposure, account_daily_pnl=account_daily_pnl)
            if not decision.allow:
                self.db.update_signal_status(signal_id, 'blocked', decision.reason)
                self.db.insert_event(
                    'signal_blocked',
                    f"策略 {sig.strategy_id} 信号被风控拦截",
                    {
                        'signal_id': signal_id,
                        'strategy_id': sig.strategy_id,
                        'market_id': sig.market_id,
                        'token_id': sig.token_id,
                        'reason': decision.reason,
                    },
                )
                blocked += 1
                continue

            if cfg.dry_run:
                self.db.update_signal_status(signal_id, 'dry_run', 'dry_run=true')
                self.db.insert_event(
                    'signal_dry_run',
                    f"策略 {sig.strategy_id} 信号 dry-run",
                    {
                        'signal_id': signal_id,
                        'strategy_id': sig.strategy_id,
                        'token_id': sig.token_id,
                        'size_usdc': decision.size_usdc,
                    },
                )
                continue

            try:
                res = self.execution_engine.execute(
                    signal=row,
                    signal_id=signal_id,
                    size_usdc=decision.size_usdc,
                    mode=cfg.mode,
                )
                self.db.update_signal_status(signal_id, 'executed', 'ok')
                self.db.insert_event(
                    'signal_executed',
                    f"策略 {sig.strategy_id} 执行完成",
                    {
                        'signal_id': signal_id,
                        'strategy_id': sig.strategy_id,
                        'token_id': sig.token_id,
                        'side': sig.side,
                        'mode': cfg.mode,
                        'fills_count': _safe_int(res.get('fills_count', 0)),
                        'pnl_delta': float(res.get('pnl_delta', 0.0)),
                    },
                )
                executed += 1
                total_exposure += decision.size_usdc
                pnl_delta = float(res.get('pnl_delta', 0.0))
                self.risk_engine.record_trade_result(sig.strategy_id, pnl_delta=pnl_delta)
            except Exception as exc:
                self.db.update_signal_status(signal_id, 'failed', str(exc))
                self.db.insert_event(
                    'signal_failed',
                    f"策略 {sig.strategy_id} 执行失败",
                    {
                        'signal_id': signal_id,
                        'strategy_id': sig.strategy_id,
                        'token_id': sig.token_id,
                        'error': str(exc),
                    },
                )
                failed += 1

        summary = {
            'time_utc': _now_utc(),
            'mode': cfg.mode,
            'market_count': len(markets),
            'tracked_tokens': len(books),
            'signals_created': created,
            'signals_executed': executed,
            'signals_blocked': blocked,
            'signals_failed': failed,
            'signals_dropped_no_book': dropped_no_book,
            'dry_run': bool(cfg.dry_run),
            'enforce_live_gate': bool(cfg.enforce_live_gate),
            'market_refresh': {
                'markets': int(market_refresh.get('markets', 0)),
                'tokens': int(market_refresh.get('tokens', 0)),
                'updated_at_utc': market_refresh.get('updated_at_utc', ''),
            },
            'risk_account_daily_pnl': account_daily_pnl,
            'total_exposure': total_exposure,
        }
        if cfg.mode == 'live' and cfg.enforce_live_gate:
            eligible_rows = [x for x in live_gate_map.values() if bool(x.get('eligible', False))]
            summary['live_gate'] = {
                'strategy_count': len(live_gate_map),
                'eligible_count': len(eligible_rows),
                'min_hours': int(cfg.live_gate_min_hours),
                'min_fills': int(cfg.live_gate_min_fills),
                'min_pnl': float(cfg.live_gate_min_pnl),
                'min_win_rate': float(cfg.live_gate_min_win_rate),
            }
        self.db.insert_event('cycle_done', '自动量化轮次完成', summary)
        return summary

    def run_once(self, cfg: OrchestratorConfig | None = None) -> dict[str, Any]:
        use_cfg = cfg or self._cfg
        summary = self._cycle_once(use_cfg)
        self._set_status(last_summary=summary, phase='idle')
        return summary

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                cfg = OrchestratorConfig(**asdict(self._cfg))
                cycle = int(self._status.get('cycle', 0)) + 1
            self._set_status(cycle=cycle)
            try:
                summary = self._cycle_once(cfg)
                self._set_status(last_summary=summary, phase='sleeping', last_error='')
            except Exception as exc:
                self._set_status(phase='failed', last_error=str(exc))
                self.db.insert_event('cycle_error', '自动量化轮次失败', {'error': str(exc)})
            wait_sec = max(2, int(cfg.cycle_sec))
            deadline = time.monotonic() + wait_sec
            while time.monotonic() < deadline and not self._stop.is_set():
                time.sleep(0.25)
        self._set_status(running=False, phase='idle')
        self.db.insert_event('orchestrator_stop', '自动量化调度器已停止', {})

    def start(self, cfg: OrchestratorConfig) -> dict[str, Any]:
        with self._lock:
            running = bool(self._thread and self._thread.is_alive() and not self._stop.is_set())
            if running:
                return {'ok': False, 'reason': 'already_running', **self.status()}
            self._cfg = cfg
            self._stop.clear()
            self._status = {
                'running': True,
                'started_at_utc': _now_utc(),
                'updated_at_utc': _now_utc(),
                'cycle': 0,
                'phase': 'starting',
                'last_error': '',
                'last_summary': {},
            }
            self._thread = threading.Thread(target=self._run_loop, daemon=True)
            self._thread.start()
        self.db.insert_event('orchestrator_start', '自动量化调度器启动', asdict(cfg))
        return {'ok': True, **self.status()}

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._stop.set()
            t = self._thread
        if t and t.is_alive():
            t.join(timeout=8)
        return {'ok': True, **self.status()}
