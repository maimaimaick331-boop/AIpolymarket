from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from libs.quant.db import QuantDB


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _parse_utc(v: Any) -> datetime | None:
    s = str(v or '').strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class RiskDecision:
    allow: bool
    size_usdc: float
    reason: str
    strategy_id: str
    size_scale: float
    account_daily_pnl: float
    strategy_daily_pnl: float
    total_exposure: float


class RiskEngine:
    def __init__(
        self,
        *,
        db: QuantDB,
        paper_engine: Any,
        max_order_usdc: float = 25.0,
        max_total_exposure_usdc: float = 500.0,
        strategy_daily_loss_limit: float = -50.0,
        account_daily_loss_limit: float = -100.0,
        loss_streak_limit: int = 5,
        reduced_size_scale: float = 0.5,
        race_enabled: bool = True,
        race_min_fills: int = 12,
        race_min_win_rate: float = 0.40,
        race_min_pnl: float = 0.0,
        race_lookback_hours: int = 24,
    ) -> None:
        self.db = db
        self.paper_engine = paper_engine
        self.max_order_usdc = max(1.0, float(max_order_usdc))
        self.max_total_exposure_usdc = max(10.0, float(max_total_exposure_usdc))
        self.strategy_daily_loss_limit = float(strategy_daily_loss_limit)
        self.account_daily_loss_limit = float(account_daily_loss_limit)
        self.loss_streak_limit = max(1, int(loss_streak_limit))
        self.reduced_size_scale = max(0.1, min(1.0, float(reduced_size_scale)))
        self.race_enabled = bool(race_enabled)
        self.race_min_fills = max(1, int(race_min_fills))
        self.race_min_win_rate = max(0.0, min(1.0, float(race_min_win_rate)))
        self.race_min_pnl = float(race_min_pnl)
        self.race_lookback_hours = max(1, int(race_lookback_hours))

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime('%Y-%m-%d')

    def _compute_total_exposure(self, status: dict[str, Any]) -> float:
        total = 0.0
        rows = status.get('leaderboard', []) if isinstance(status, dict) else []
        if not isinstance(rows, list):
            return 0.0
        for row in rows:
            if not isinstance(row, dict):
                continue
            positions = row.get('positions', [])
            if not isinstance(positions, list):
                continue
            for pos in positions:
                if not isinstance(pos, dict):
                    continue
                qty = _safe_float(pos.get('qty', 0.0))
                mark = _safe_float(pos.get('mark_price', 0.0))
                total += abs(qty * mark)
        return total

    def refresh_from_paper(self) -> dict[str, Any]:
        status = self.paper_engine.status(limit=1000)
        today = self._today()
        total_pnl = _safe_float(((status or {}).get('totals', {}) or {}).get('total_pnl', 0.0))

        acct = self.db.account_risk()
        if str(acct.get('daily_date', '')) != today:
            acct['daily_date'] = today
            acct['daily_pnl'] = 0.0
            acct['trading_enabled'] = 1
            acct['stop_reason'] = ''
            acct['stop_until_utc'] = ''

        acct['daily_pnl'] = total_pnl
        stop_until = _parse_utc(acct.get('stop_until_utc', ''))
        if stop_until is not None and datetime.now(timezone.utc) > stop_until:
            acct['trading_enabled'] = 1
            acct['stop_reason'] = ''
            acct['stop_until_utc'] = ''

        if total_pnl <= self.account_daily_loss_limit:
            acct['trading_enabled'] = 0
            acct['stop_reason'] = f'账户日亏损触发 {self.account_daily_loss_limit}'
            acct['stop_until_utc'] = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        acct['updated_at_utc'] = _now_utc()
        self.db.update_account_risk(acct)

        rows = status.get('leaderboard', []) if isinstance(status, dict) else []
        if not isinstance(rows, list):
            rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            sid = str(row.get('strategy_id', '')).strip()
            if not sid:
                continue
            sr = self.db.strategy_risk(sid)
            if str(sr.get('daily_date', '')) != today:
                sr['daily_date'] = today
                sr['daily_pnl'] = 0.0
                sr['consecutive_losses'] = 0
                sr['size_scale'] = 1.0
                sr['paused_until_utc'] = ''
            sr['daily_pnl'] = _safe_float(row.get('total_pnl', 0.0))
            paused = _parse_utc(sr.get('paused_until_utc', ''))
            if paused is not None and datetime.now(timezone.utc) > paused:
                sr['paused_until_utc'] = ''
            if _safe_float(sr.get('daily_pnl', 0.0)) <= self.strategy_daily_loss_limit:
                sr['paused_until_utc'] = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
            if _safe_int(sr.get('consecutive_losses', 0)) >= self.loss_streak_limit:
                sr['size_scale'] = self.reduced_size_scale
            sr['updated_at_utc'] = _now_utc()
            self.db.upsert_strategy_risk(sid, sr)

        return {
            'status': status,
            'account_risk': self.db.account_risk(),
            'strategy_risk': self.db.list_strategy_risk(),
            'total_exposure': self._compute_total_exposure(status),
            'updated_at_utc': _now_utc(),
        }

    def evaluate_signal(self, signal: dict[str, Any], total_exposure: float, account_daily_pnl: float) -> RiskDecision:
        sid = str(signal.get('strategy_id', '')).strip() or 'unknown'
        sr = self.db.strategy_risk(sid)
        size_scale = max(0.1, min(1.0, _safe_float(sr.get('size_scale', 1.0), 1.0)))
        sr_pnl = _safe_float(sr.get('daily_pnl', 0.0))
        suggested = max(0.0, _safe_float(signal.get('suggested_notional', 0.0)))

        acct = self.db.account_risk()
        enabled = bool(_safe_int(acct.get('trading_enabled', 1), 1))
        if not enabled:
            return RiskDecision(
                allow=False,
                size_usdc=0.0,
                reason=str(acct.get('stop_reason', 'account_stop')),
                strategy_id=sid,
                size_scale=size_scale,
                account_daily_pnl=account_daily_pnl,
                strategy_daily_pnl=sr_pnl,
                total_exposure=total_exposure,
            )

        paused = _parse_utc(sr.get('paused_until_utc', ''))
        if paused is not None and datetime.now(timezone.utc) <= paused:
            return RiskDecision(
                allow=False,
                size_usdc=0.0,
                reason=f'strategy_paused_until={paused.isoformat()}',
                strategy_id=sid,
                size_scale=size_scale,
                account_daily_pnl=account_daily_pnl,
                strategy_daily_pnl=sr_pnl,
                total_exposure=total_exposure,
            )

        if sr_pnl <= self.strategy_daily_loss_limit:
            return RiskDecision(
                allow=False,
                size_usdc=0.0,
                reason=f'strategy_daily_loss_limit={self.strategy_daily_loss_limit}',
                strategy_id=sid,
                size_scale=size_scale,
                account_daily_pnl=account_daily_pnl,
                strategy_daily_pnl=sr_pnl,
                total_exposure=total_exposure,
            )

        if account_daily_pnl <= self.account_daily_loss_limit:
            return RiskDecision(
                allow=False,
                size_usdc=0.0,
                reason=f'account_daily_loss_limit={self.account_daily_loss_limit}',
                strategy_id=sid,
                size_scale=size_scale,
                account_daily_pnl=account_daily_pnl,
                strategy_daily_pnl=sr_pnl,
                total_exposure=total_exposure,
            )

        if suggested <= 0:
            suggested = self.max_order_usdc
        size_usdc = min(self.max_order_usdc, suggested) * size_scale
        if size_usdc <= 0:
            return RiskDecision(
                allow=False,
                size_usdc=0.0,
                reason='size_zero_after_scale',
                strategy_id=sid,
                size_scale=size_scale,
                account_daily_pnl=account_daily_pnl,
                strategy_daily_pnl=sr_pnl,
                total_exposure=total_exposure,
            )

        if total_exposure + size_usdc > self.max_total_exposure_usdc:
            return RiskDecision(
                allow=False,
                size_usdc=0.0,
                reason=f'exposure_limit={self.max_total_exposure_usdc}',
                strategy_id=sid,
                size_scale=size_scale,
                account_daily_pnl=account_daily_pnl,
                strategy_daily_pnl=sr_pnl,
                total_exposure=total_exposure,
            )

        return RiskDecision(
            allow=True,
            size_usdc=size_usdc,
            reason='ok',
            strategy_id=sid,
            size_scale=size_scale,
            account_daily_pnl=account_daily_pnl,
            strategy_daily_pnl=sr_pnl,
            total_exposure=total_exposure,
        )

    def record_trade_result(self, strategy_id: str, pnl_delta: float) -> None:
        sid = str(strategy_id or '').strip()
        if not sid:
            return
        row = self.db.strategy_risk(sid)
        losses = _safe_int(row.get('consecutive_losses', 0))
        if pnl_delta < -1e-9:
            losses += 1
        elif pnl_delta > 1e-9:
            losses = 0
        row['consecutive_losses'] = losses
        row['size_scale'] = self.reduced_size_scale if losses >= self.loss_streak_limit else 1.0
        row['updated_at_utc'] = _now_utc()
        self.db.upsert_strategy_risk(sid, row)

    def snapshot(self) -> dict[str, Any]:
        race_stats = self.db.strategy_performance(mode='paper', hours=self.race_lookback_hours)
        return {
            'account': self.db.account_risk(),
            'strategies': self.db.list_strategy_risk(),
            'limits': {
                'max_order_usdc': self.max_order_usdc,
                'max_total_exposure_usdc': self.max_total_exposure_usdc,
                'strategy_daily_loss_limit': self.strategy_daily_loss_limit,
                'account_daily_loss_limit': self.account_daily_loss_limit,
                'loss_streak_limit': self.loss_streak_limit,
                'reduced_size_scale': self.reduced_size_scale,
                'race_enabled': self.race_enabled,
                'race_min_fills': self.race_min_fills,
                'race_min_win_rate': self.race_min_win_rate,
                'race_min_pnl': self.race_min_pnl,
                'race_lookback_hours': self.race_lookback_hours,
            },
            'race': {
                'lookback_hours': self.race_lookback_hours,
                'count': len(race_stats),
                'rows': race_stats,
            },
            'updated_at_utc': _now_utc(),
        }

    def paper_strategy_gate(self, strategy_id: str) -> tuple[bool, str]:
        sid = str(strategy_id or '').strip()
        if not sid:
            return False, 'strategy_id_empty'
        if not self.race_enabled:
            return True, 'race_disabled'
        rows = self.db.strategy_performance(mode='paper', hours=self.race_lookback_hours)
        target = None
        for row in rows:
            if str(row.get('strategy_id', '')) == sid:
                target = row
                break
        if target is None:
            return True, 'race_warmup_no_fills'
        fills_count = _safe_int(target.get('fills_count', 0))
        if fills_count < self.race_min_fills:
            return True, f'race_warmup_fills<{self.race_min_fills}'
        pnl = _safe_float(target.get('pnl_total', 0.0))
        win_rate = _safe_float(target.get('win_rate', 0.0))
        if pnl < self.race_min_pnl:
            return False, f'race_eliminate_pnl<{self.race_min_pnl}'
        if win_rate < self.race_min_win_rate:
            return False, f'race_eliminate_win_rate<{self.race_min_win_rate}'
        return True, 'race_ok'
