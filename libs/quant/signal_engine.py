from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
import json
import math
import os
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from libs.quant.db import QuantDB
from libs.services.model_router import ModelAllocation, ModelRouterStore, choose_provider, normalize_extra_headers


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _json_block(text: str) -> dict[str, Any]:
    s = str(text or '').strip()
    if not s:
        return {}
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    left = s.find('{')
    right = s.rfind('}')
    if left >= 0 and right > left:
        try:
            obj = json.loads(s[left : right + 1])
            if isinstance(obj, dict):
                return obj
        except Exception:
            return {}
    return {}


def _align_tick(price: float, tick: float, side: str) -> float:
    p = max(0.0001, float(price))
    t = max(0.0000001, float(tick))
    units = p / t
    if str(side or '').upper() == 'BUY':
        v = math.floor(units + 1e-9) * t
    else:
        v = math.ceil(units - 1e-9) * t
    return max(0.0001, round(v, 8))


def _workshop_eval_condition_value(actual: float, operator: str, threshold: float) -> bool:
    op = str(operator or '').strip()
    if op == '>=':
        return actual >= threshold
    if op == '<=':
        return actual <= threshold
    if op == '>':
        return actual > threshold
    if op == '<':
        return actual < threshold
    if op == '==':
        return abs(actual - threshold) <= 1e-9
    return False


def _workshop_direction(direction: str) -> str:
    d = str(direction or '').strip().lower()
    if d in {'buy_yes', 'buy_no', 'both', 'market_make'}:
        return d
    if d in {'做多yes', 'long_yes'}:
        return 'buy_yes'
    if d in {'做空yes', 'long_no'}:
        return 'buy_no'
    if d in {'双向', 'two_sided'}:
        return 'both'
    if d in {'做市', 'market_making'}:
        return 'market_make'
    return 'both'


def execute_workshop_strategy(
    strategy_config: dict[str, Any],
    market: dict[str, Any],
    *,
    ai_eval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate one workshop strategy against one market and return a structured decision."""
    cfg = strategy_config if isinstance(strategy_config, dict) else {}
    cond_rows = cfg.get('trigger_conditions', [])
    if not isinstance(cond_rows, list):
        cond_rows = []
    m = market if isinstance(market, dict) else {}
    ai = ai_eval if isinstance(ai_eval, dict) else {}

    yes_bid = _safe_float(m.get('yes_best_bid', 0.0))
    yes_ask = _safe_float(m.get('yes_best_ask', 0.0))
    no_bid = _safe_float(m.get('no_best_bid', 0.0))
    no_ask = _safe_float(m.get('no_best_ask', 0.0))
    market_price = _safe_float(m.get('yes_mid', 0.0))
    if market_price <= 0 and yes_bid > 0 and yes_ask > 0:
        market_price = (yes_bid + yes_ask) / 2.0
    volume = _safe_float(m.get('volume', 0.0))
    spread = max(_safe_float(m.get('yes_spread', 0.0)), _safe_float(m.get('no_spread', 0.0)))
    yes_no_sum = _safe_float(m.get('yes_no_sum', 0.0))
    if yes_no_sum <= 0 and yes_ask > 0 and no_ask > 0:
        yes_no_sum = yes_ask + no_ask
    arb_gap = abs(yes_no_sum - 1.0) if yes_no_sum > 0 else 0.0

    ai_prob = _safe_float(ai.get('probability', ai.get('ai_probability', 0.0)), 0.0)
    ai_conf = _safe_float(ai.get('confidence', ai.get('ai_confidence', 0.0)), 0.0)
    deviation = abs(ai_prob - market_price) if ai_prob > 0 and market_price > 0 else 0.0

    checks: list[dict[str, Any]] = []
    for row in cond_rows:
        if not isinstance(row, dict):
            continue
        ctype = str(row.get('type', '')).strip().lower()
        op = str(row.get('operator', '')).strip()
        threshold = _safe_float(row.get('value', 0.0))
        actual = 0.0
        if ctype == 'spread_threshold':
            actual = spread
        elif ctype == 'ai_deviation':
            actual = deviation
        elif ctype == 'arb_gap':
            actual = arb_gap
        elif ctype == 'volume_filter':
            actual = volume
        elif ctype == 'price_range':
            actual = market_price
        else:
            continue
        passed = _workshop_eval_condition_value(actual, op, threshold)
        checks.append(
            {
                'type': ctype,
                'operator': op,
                'threshold': threshold,
                'actual': actual,
                'passed': passed,
                'description': str(row.get('description', ctype)),
            }
        )

    triggered = bool(checks) and all(bool(x.get('passed', False)) for x in checks)
    direction = _workshop_direction(str(cfg.get('direction', 'both')))
    decision = 'hold'
    if triggered:
        if direction == 'buy_yes':
            decision = 'buy_yes'
        elif direction == 'buy_no':
            decision = 'buy_no'
        elif direction == 'market_make':
            decision = 'market_make'
        else:
            if ai_prob > 0 and market_price > 0:
                decision = 'buy_yes' if ai_prob >= market_price else 'buy_no'
            else:
                decision = 'buy_yes'

    return {
        'market_id': str(m.get('market_id', '')),
        'market_name': str(m.get('question', m.get('market_name', ''))),
        'triggered': triggered,
        'decision': decision,
        'checks': checks,
        'market_price': market_price,
        'spread': spread,
        'volume_24h': volume,
        'yes_no_sum': yes_no_sum,
        'arb_gap': arb_gap,
        'ai_probability': ai_prob,
        'ai_confidence': ai_conf,
        'deviation': deviation,
    }


@dataclass
class QuantSignal:
    time_utc: str
    strategy_id: str
    signal_type: str
    market_id: str
    token_id: str
    side: str
    order_kind: str
    price: float
    confidence: float
    score: float
    suggested_notional: float
    reason: dict[str, Any]

    def to_row(self) -> dict[str, Any]:
        return asdict(self)


class StrategySignalEngine:
    def __init__(
        self,
        *,
        db: QuantDB,
        router_store: ModelRouterStore,
        event_hook: Any | None = None,
        paper_engine: Any | None = None,
        arb_buy_threshold: float = 0.96,
        arb_sell_threshold: float = 1.04,
        fee_buffer: float = 0.02,
        mm_liq_min: float = 1000.0,
        mm_liq_max: float = 50000.0,
        mm_min_spread: float = 0.05,
        mm_min_volume: float = 1000.0,
        mm_min_depth_usdc: float = 500.0,
        mm_min_market_count: int = 10,
        mm_target_market_count: int = 12,
        mm_max_single_side_position_usdc: float = 50.0,
        mm_max_position_per_market_usdc: float = 50.0,
        mm_inventory_skew_strength: float = 1.0,
        mm_allow_short_sell: bool = False,
        mm_taker_rebalance: bool = False,
        ai_deviation_threshold: float = 0.10,
        ai_min_confidence: float = 0.50,
        ai_eval_interval_sec: int = 900,
        ai_max_markets_per_cycle: int = 6,
    ) -> None:
        self.db = db
        self.router_store = router_store
        self.event_hook = event_hook
        self.paper_engine = paper_engine
        self.arb_buy_threshold = float(arb_buy_threshold)
        self.arb_sell_threshold = float(arb_sell_threshold)
        self.fee_buffer = max(0.0, float(fee_buffer))
        self.mm_liq_min = float(mm_liq_min)
        self.mm_liq_max = float(mm_liq_max)
        self.mm_min_spread = max(0.04, float(mm_min_spread))
        self.mm_min_volume = max(0.0, float(mm_min_volume))
        self.mm_min_depth_usdc = max(0.0, float(mm_min_depth_usdc))
        self.mm_min_market_count = max(1, int(mm_min_market_count))
        self.mm_target_market_count = max(self.mm_min_market_count, int(mm_target_market_count))
        self.mm_max_single_side_position_usdc = max(1.0, float(mm_max_single_side_position_usdc))
        self.mm_max_position_per_market_usdc = max(1.0, float(mm_max_position_per_market_usdc))
        self.mm_inventory_skew_strength = max(0.0, float(mm_inventory_skew_strength))
        self.mm_allow_short_sell = bool(mm_allow_short_sell)
        self.mm_taker_rebalance = bool(mm_taker_rebalance)
        self.ai_deviation_threshold = float(ai_deviation_threshold)
        self.ai_min_confidence = float(ai_min_confidence)
        self.ai_eval_interval_sec = max(60, int(ai_eval_interval_sec))
        # AI 评估默认覆盖更大市场集合，保障机会发现密度。
        self.ai_max_markets_per_cycle = max(50, int(ai_max_markets_per_cycle))

    def _emit_event(self, kind: str, message: str, payload: dict[str, Any] | None = None) -> None:
        cb = self.event_hook
        if cb is None:
            return
        try:
            cb(str(kind), str(message), payload or {})
        except Exception:
            return

    def update_limits(self, **kwargs: Any) -> None:
        if 'arb_buy_threshold' in kwargs:
            self.arb_buy_threshold = float(kwargs['arb_buy_threshold'])
        if 'arb_sell_threshold' in kwargs:
            self.arb_sell_threshold = float(kwargs['arb_sell_threshold'])
        if 'fee_buffer' in kwargs:
            self.fee_buffer = max(0.0, float(kwargs['fee_buffer']))
        if 'mm_liq_min' in kwargs:
            self.mm_liq_min = float(kwargs['mm_liq_min'])
        if 'mm_liq_max' in kwargs:
            self.mm_liq_max = float(kwargs['mm_liq_max'])
        if 'mm_min_spread' in kwargs:
            self.mm_min_spread = max(0.04, float(kwargs['mm_min_spread']))
        if 'mm_min_volume' in kwargs:
            self.mm_min_volume = max(0.0, float(kwargs['mm_min_volume']))
        if 'mm_min_depth_usdc' in kwargs:
            self.mm_min_depth_usdc = max(0.0, float(kwargs['mm_min_depth_usdc']))
        if 'mm_min_market_count' in kwargs:
            self.mm_min_market_count = max(1, int(kwargs['mm_min_market_count']))
            self.mm_target_market_count = max(self.mm_target_market_count, self.mm_min_market_count)
        if 'mm_target_market_count' in kwargs:
            self.mm_target_market_count = max(self.mm_min_market_count, int(kwargs['mm_target_market_count']))
        if 'mm_max_single_side_position_usdc' in kwargs:
            self.mm_max_single_side_position_usdc = max(1.0, float(kwargs['mm_max_single_side_position_usdc']))
        if 'mm_max_position_per_market_usdc' in kwargs:
            self.mm_max_position_per_market_usdc = max(1.0, float(kwargs['mm_max_position_per_market_usdc']))
        if 'mm_inventory_skew_strength' in kwargs:
            self.mm_inventory_skew_strength = max(0.0, float(kwargs['mm_inventory_skew_strength']))
        if 'mm_allow_short_sell' in kwargs:
            self.mm_allow_short_sell = bool(kwargs['mm_allow_short_sell'])
        if 'mm_taker_rebalance' in kwargs:
            self.mm_taker_rebalance = bool(kwargs['mm_taker_rebalance'])
        if 'ai_deviation_threshold' in kwargs:
            self.ai_deviation_threshold = float(kwargs['ai_deviation_threshold'])
        if 'ai_min_confidence' in kwargs:
            self.ai_min_confidence = float(kwargs['ai_min_confidence'])
        if 'ai_eval_interval_sec' in kwargs:
            self.ai_eval_interval_sec = max(60, int(kwargs['ai_eval_interval_sec']))
        if 'ai_max_markets_per_cycle' in kwargs:
            self.ai_max_markets_per_cycle = max(50, int(kwargs['ai_max_markets_per_cycle']))

    @staticmethod
    def _provider_priority(provider_id: str) -> tuple[int, str]:
        pid = str(provider_id or '').strip()
        if pid in {'yunwu-88033', 'yunwu-237131'}:
            return (10, pid)
        if pid in {'yunwu-80033', 'yunwu-56866'}:
            return (20, pid)
        return (100, pid)

    def _resolve_ai_providers(self, cfg: ModelAllocation, provider_id: str = '') -> list[Any]:
        rows = [p for p in (cfg.providers or []) if bool(getattr(p, 'enabled', False)) and str(getattr(p, 'endpoint', '') or '').strip()]
        if not rows:
            return []
        preferred = str(provider_id or '').strip()
        if preferred:
            hit = [p for p in rows if str(getattr(p, 'provider_id', '')).strip() == preferred]
            if hit:
                tail = [p for p in rows if str(getattr(p, 'provider_id', '')).strip() != preferred]
                tail.sort(key=lambda x: self._provider_priority(str(getattr(x, 'provider_id', ''))))
                return hit + tail
        rows.sort(key=lambda x: self._provider_priority(str(getattr(x, 'provider_id', ''))))
        return rows

    def generate(
        self,
        *,
        markets: list[dict[str, Any]],
        books: dict[str, dict[str, Any]],
        provider_id: str = '',
        ai_prompt: str = '',
        enable_arb: bool = True,
        enable_mm: bool = True,
        enable_ai: bool = True,
    ) -> list[QuantSignal]:
        out: list[QuantSignal] = []
        if enable_arb:
            out.extend(self._arb_signals(markets))
        if enable_mm:
            out.extend(self._mm_signals(markets))
        if enable_ai:
            out.extend(self._ai_signals(markets, books=books, provider_id=provider_id, prompt=ai_prompt))
        return out

    def _arb_signals(self, markets: list[dict[str, Any]]) -> list[QuantSignal]:
        out: list[QuantSignal] = []
        now = _now_utc()
        trigger_buy = min(self.arb_buy_threshold, 1.0 - self.fee_buffer * 2.0)
        trigger_sell = max(self.arb_sell_threshold, 1.0 + self.fee_buffer * 2.0)
        min_buy_cost = 999.0
        max_sell_rev = -1.0
        for m in markets:
            market_id = str(m.get('market_id') or m.get('id') or '')
            market_name = str(m.get('question') or market_id).strip() or market_id
            yes_token = str(m.get('yes_token_id', '')).strip()
            no_token = str(m.get('no_token_id', '')).strip()
            if not market_id or not yes_token or not no_token:
                continue
            y_ask = _safe_float(m.get('yes_best_ask', 0.0))
            n_ask = _safe_float(m.get('no_best_ask', 0.0))
            y_bid = _safe_float(m.get('yes_best_bid', 0.0))
            n_bid = _safe_float(m.get('no_best_bid', 0.0))
            buy_cost = y_ask + n_ask if y_ask > 0 and n_ask > 0 else 999.0
            sell_rev = y_bid + n_bid if y_bid > 0 and n_bid > 0 else -1.0
            min_buy_cost = min(min_buy_cost, buy_cost)
            max_sell_rev = max(max_sell_rev, sell_rev)
            buy_triggered = buy_cost < trigger_buy
            sell_triggered = sell_rev > trigger_sell
            self._emit_event(
                'arb_scan',
                f"[ARB_SCAN] market={market_name} yes_ask={y_ask:.4f} no_ask={n_ask:.4f} sum={buy_cost:.4f} "
                f"threshold={trigger_buy:.4f} triggered={str(buy_triggered).lower()} "
                f"yes_bid={y_bid:.4f} no_bid={n_bid:.4f} sum_bid={sell_rev:.4f} "
                f"sell_threshold={trigger_sell:.4f} sell_triggered={str(sell_triggered).lower()}",
                {
                    'market_id': market_id,
                    'market_name': market_name,
                    'yes_ask': y_ask,
                    'no_ask': n_ask,
                    'yes_bid': y_bid,
                    'no_bid': n_bid,
                    'buy_sum': buy_cost,
                    'sell_sum': sell_rev,
                    'buy_threshold': trigger_buy,
                    'sell_threshold': trigger_sell,
                    'buy_triggered': buy_triggered,
                    'sell_triggered': sell_triggered,
                },
            )

            if buy_cost < trigger_buy:
                edge = max(0.0, 1.0 - buy_cost - self.fee_buffer)
                conf = min(0.98, 0.65 + edge * 10.0)
                for token_id, px, outcome in ((yes_token, y_ask, 'Yes'), (no_token, n_ask, 'No')):
                    out.append(
                        QuantSignal(
                            time_utc=now,
                            strategy_id='arb_detector',
                            signal_type='arbitrage_buy_pair',
                            market_id=market_id,
                            token_id=token_id,
                            side='BUY',
                            order_kind='market',
                            price=px,
                            confidence=conf,
                            score=edge,
                            suggested_notional=20.0,
                            reason={
                                'rule': 'yes_ask + no_ask < threshold_with_fee',
                                'yes_ask': y_ask,
                                'no_ask': n_ask,
                                'pair_cost': buy_cost,
                                'trigger': trigger_buy,
                                'outcome': outcome,
                                'decision_text': f"Yes+No买入价和={buy_cost:.4f} < 阈值={trigger_buy:.4f}",
                            },
                        )
                    )

            if sell_rev > trigger_sell:
                edge = max(0.0, sell_rev - 1.0 - self.fee_buffer)
                conf = min(0.98, 0.63 + edge * 10.0)
                for token_id, px, outcome in ((yes_token, y_bid, 'Yes'), (no_token, n_bid, 'No')):
                    out.append(
                        QuantSignal(
                            time_utc=now,
                            strategy_id='arb_detector',
                            signal_type='arbitrage_sell_pair',
                            market_id=market_id,
                            token_id=token_id,
                            side='SELL',
                            order_kind='market',
                            price=px,
                            confidence=conf,
                            score=edge,
                            suggested_notional=20.0,
                            reason={
                                'rule': 'yes_bid + no_bid > threshold_with_fee',
                                'yes_bid': y_bid,
                                'no_bid': n_bid,
                                'pair_bid_sum': sell_rev,
                                'trigger': trigger_sell,
                                'outcome': outcome,
                                'decision_text': f"Yes+No卖出价和={sell_rev:.4f} > 阈值={trigger_sell:.4f}",
                            },
                        )
                    )
        self._emit_event(
            'arb_scan_summary',
            f"[ARB_SCAN] summary markets={len(markets)} min_buy_sum={min_buy_cost:.4f} max_sell_sum={max_sell_rev:.4f} "
            f"buy_threshold={trigger_buy:.4f} sell_threshold={trigger_sell:.4f}",
            {
                'markets': len(markets),
                'min_buy_sum': min_buy_cost,
                'max_sell_sum': max_sell_rev,
                'buy_threshold': trigger_buy,
                'sell_threshold': trigger_sell,
            },
        )
        return out

    def _mm_signals(self, markets: list[dict[str, Any]]) -> list[QuantSignal]:
        out: list[QuantSignal] = []
        now = _now_utc()
        scan_spread_floor = 0.04
        signal_spread_floor = max(0.04, float(self.mm_min_spread))
        market_rows: list[dict[str, Any]] = []
        for m in markets:
            market_id = str(m.get('market_id') or m.get('id') or '').strip()
            if not market_id:
                continue
            liq = _safe_float(m.get('liquidity', 0.0))
            volume = _safe_float(m.get('volume', 0.0))
            yes_spread = _safe_float(m.get('yes_spread', 0.0))
            no_spread = _safe_float(m.get('no_spread', 0.0))
            spread_max = max(yes_spread, no_spread)
            yes_mid = _safe_float(m.get('yes_mid', 0.0))
            no_mid = _safe_float(m.get('no_mid', 0.0))
            yes_depth = _safe_float(m.get('yes_depth_bid', 0.0)) + _safe_float(m.get('yes_depth_ask', 0.0))
            no_depth = _safe_float(m.get('no_depth_bid', 0.0)) + _safe_float(m.get('no_depth_ask', 0.0))
            yes_depth_usdc = yes_depth * max(yes_mid, 0.01)
            no_depth_usdc = no_depth * max(no_mid, 0.01)
            depth_usdc = yes_depth_usdc + no_depth_usdc
            row = dict(m)
            row['market_id'] = market_id
            row['market_name'] = str(m.get('question') or market_id).strip() or market_id
            row['spread_max'] = spread_max
            row['depth_usdc'] = depth_usdc
            row['volume'] = volume
            row['liquidity'] = liq
            market_rows.append(row)

        strict_candidates = [
            x
            for x in market_rows
            if x['liquidity'] >= self.mm_liq_min
            and x['liquidity'] <= self.mm_liq_max
            and x['volume'] >= self.mm_min_volume
            and x['depth_usdc'] >= self.mm_min_depth_usdc
            and x['spread_max'] >= scan_spread_floor
        ]
        strict_candidates.sort(key=lambda x: (float(x.get('spread_max', 0.0)), float(x.get('volume', 0.0))), reverse=True)
        selected: list[dict[str, Any]] = strict_candidates[: self.mm_target_market_count]
        insufficient = len(strict_candidates) < self.mm_min_market_count

        self._emit_event(
            'mm_scan',
            f"[MM_SCAN] scanned={len(market_rows)} strict={len(strict_candidates)} selected={len(selected)} "
            f"scan_spread_floor={scan_spread_floor:.4f} signal_min_spread={signal_spread_floor:.4f} "
            f"min_volume={self.mm_min_volume:.2f} min_depth={self.mm_min_depth_usdc:.2f} "
            f"insufficient={str(insufficient).lower()}",
            {
                'scanned_markets': len(market_rows),
                'strict_candidates': len(strict_candidates),
                'selected_markets': len(selected),
                'watch_only_markets': 0,
                'insufficient_markets': insufficient,
                'scan_spread_floor': scan_spread_floor,
                'signal_min_spread': signal_spread_floor,
                'min_volume': self.mm_min_volume,
                'min_depth_usdc': self.mm_min_depth_usdc,
                'selected_market_ids': [str(x.get('market_id', '')) for x in selected],
                'selected_market_names': [str(x.get('market_name', '')) for x in selected],
            },
        )
        if insufficient:
            self._emit_event(
                'mm_scan_insufficient',
                (
                    f"[MM_SCAN] strict_candidates={len(strict_candidates)} < mm_min_market_count={self.mm_min_market_count}, "
                    "skip market making for this cycle"
                ),
                {
                    'strict_candidates': len(strict_candidates),
                    'mm_min_market_count': self.mm_min_market_count,
                    'selected_market_ids': [str(x.get('market_id', '')) for x in selected],
                },
            )
            return []

        pos_notional_by_token: dict[str, float] = {}
        pos_qty_by_token: dict[str, float] = {}
        market_abs_notional: dict[str, float] = {}
        token_to_market: dict[str, str] = {}
        for m in selected:
            mid = str(m.get('market_id', ''))
            y = str(m.get('yes_token_id', '')).strip()
            n = str(m.get('no_token_id', '')).strip()
            if y:
                token_to_market[y] = mid
            if n:
                token_to_market[n] = mid
        if self.paper_engine is not None:
            try:
                positions = self.paper_engine.list_positions(strategy_id='market_maker')
            except Exception:
                positions = []
            if isinstance(positions, list):
                for row in positions:
                    if not isinstance(row, dict):
                        continue
                    token_id = str(row.get('token_id', '')).strip()
                    if not token_id:
                        continue
                    qty = _safe_float(row.get('qty', 0.0))
                    mark = _safe_float(row.get('mark_price', 0.0))
                    notional = qty * mark
                    pos_notional_by_token[token_id] = notional
                    pos_qty_by_token[token_id] = qty
                    market_id = token_to_market.get(token_id, '')
                    if market_id:
                        market_abs_notional[market_id] = market_abs_notional.get(market_id, 0.0) + abs(notional)
        for m in selected:
            market_id = str(m.get('market_id') or m.get('id') or '')
            market_name = str(m.get('market_name', market_id))
            liq = _safe_float(m.get('liquidity', 0.0))
            volume = _safe_float(m.get('volume', 0.0))
            depth_usdc = _safe_float(m.get('depth_usdc', 0.0))
            market_pos_abs = _safe_float(market_abs_notional.get(market_id, 0.0))
            if not market_id:
                continue

            for token_id_key, bb_key, ba_key, spread_key in (
                ('yes_token_id', 'yes_best_bid', 'yes_best_ask', 'yes_spread'),
                ('no_token_id', 'no_best_bid', 'no_best_ask', 'no_spread'),
            ):
                token_id = str(m.get(token_id_key, '')).strip()
                if not token_id:
                    continue
                bb = _safe_float(m.get(bb_key, 0.0))
                ba = _safe_float(m.get(ba_key, 0.0))
                spread = _safe_float(m.get(spread_key, 0.0))
                if bb <= 0 or ba <= 0:
                    continue
                spread_ok = spread >= signal_spread_floor
                if not spread_ok:
                    continue
                tick = max(0.0001, _safe_float(m.get('tick_size', 0.001), 0.001))
                inv_notional = _safe_float(pos_notional_by_token.get(token_id, 0.0))
                inv_qty = _safe_float(pos_qty_by_token.get(token_id, 0.0))
                skew_ticks = int(round(self.mm_inventory_skew_strength))
                buy_px = min(0.99, bb + tick)
                sell_px = max(0.01, ba - tick)
                if inv_notional > 0:
                    # 持有多头时，降低买入侵略性、提高卖出侵略性，尽量回归库存中性。
                    inv_ratio = min(2.0, inv_notional / max(1.0, self.mm_max_single_side_position_usdc))
                    buy_px = max(0.01, bb - tick * max(1, skew_ticks + int(inv_ratio * 2)))
                    sell_px = max(bb, ba - tick * max(1, skew_ticks + 1 + int(inv_ratio * 2)))
                elif inv_notional < 0:
                    inv_ratio = min(2.0, abs(inv_notional) / max(1.0, self.mm_max_single_side_position_usdc))
                    buy_px = min(ba, bb + tick * max(1, skew_ticks + 1 + int(inv_ratio * 2)))
                    sell_px = min(0.99, ba + tick * max(1, skew_ticks + int(inv_ratio * 2)))

                if self.mm_taker_rebalance:
                    # 让模拟盘更快出现真实成交样本，便于赛马评估。
                    if inv_qty <= 0:
                        buy_px = ba
                    if inv_qty > 0:
                        sell_px = bb
                buy_px = max(0.01, min(0.99, _align_tick(buy_px, tick, 'BUY')))
                sell_px = max(0.01, min(0.99, _align_tick(sell_px, tick, 'SELL')))

                block_buy = inv_notional >= self.mm_max_single_side_position_usdc
                block_sell = inv_notional <= -self.mm_max_single_side_position_usdc
                if market_pos_abs >= self.mm_max_position_per_market_usdc:
                    block_buy = True
                    if inv_qty > 0:
                        block_sell = False
                if not self.mm_allow_short_sell and inv_qty <= 0:
                    block_sell = True
                decision_text = (
                    f"spread={spread*100:.2f}% >= min_spread={signal_spread_floor*100:.2f}% | "
                    f"depth=${depth_usdc:.2f} | volume=${volume:.2f} | market={market_name}"
                )
                out.append(
                    QuantSignal(
                        time_utc=now,
                        strategy_id='market_maker',
                        signal_type='mm_quote_buy',
                        market_id=market_id,
                        token_id=token_id,
                        side='BUY',
                        order_kind='limit',
                        price=buy_px,
                        confidence=0.58,
                        score=spread,
                        suggested_notional=15.0,
                        reason={
                            'rule': 'moderate_liquidity_quote',
                            'liquidity': liq,
                            'spread': spread,
                            'best_bid': bb,
                            'best_ask': ba,
                            'inventory_notional': inv_notional,
                            'inventory_qty': inv_qty,
                            'market_position_abs_usdc': market_pos_abs,
                            'min_spread': signal_spread_floor,
                            'spread_filter_passed': spread_ok,
                            'inventory_blocked': block_buy,
                            'decision_text': decision_text,
                        },
                    )
                )
                out.append(
                    QuantSignal(
                        time_utc=now,
                        strategy_id='market_maker',
                        signal_type='mm_quote_sell',
                        market_id=market_id,
                        token_id=token_id,
                        side='SELL',
                        order_kind='limit',
                        price=sell_px,
                        confidence=0.58,
                        score=spread,
                        suggested_notional=15.0,
                        reason={
                            'rule': 'moderate_liquidity_quote',
                            'liquidity': liq,
                            'spread': spread,
                            'best_bid': bb,
                            'best_ask': ba,
                            'inventory_notional': inv_notional,
                            'inventory_qty': inv_qty,
                            'market_position_abs_usdc': market_pos_abs,
                            'min_spread': signal_spread_floor,
                            'spread_filter_passed': spread_ok,
                            'inventory_blocked': block_sell,
                            'decision_text': decision_text,
                        },
                    )
                )
        filtered: list[QuantSignal] = []
        spread_blocked = 0
        for sig in out:
            sig_reason = sig.reason or {}
            spread = _safe_float(sig_reason.get('spread', 0.0))
            min_spread = max(0.04, _safe_float(sig_reason.get('min_spread', signal_spread_floor), signal_spread_floor))
            if spread < min_spread:
                spread_blocked += 1
                continue
            blocked = bool((sig.reason or {}).get('inventory_blocked', False))
            if blocked:
                continue
            filtered.append(sig)
        if spread_blocked > 0:
            self._emit_event(
                'mm_spread_guard',
                f"[MM_SPREAD_GUARD] blocked={spread_blocked} signals where spread < min_spread",
                {
                    'blocked': spread_blocked,
                    'signal_min_spread': signal_spread_floor,
                },
            )
        return filtered

    def _fetch_news(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        ep = str(os.getenv('NEWS_SEARCH_API_URL', '')).strip()
        if not ep:
            return []
        api_key = str(os.getenv('NEWS_SEARCH_API_KEY', '')).strip()
        url = ep
        if '?' in url:
            url = f'{url}&{urlencode({"q": query, "limit": max(1, min(limit, 10))})}'
        else:
            url = f'{url}?{urlencode({"q": query, "limit": max(1, min(limit, 10))})}'
        headers = {'Accept': 'application/json'}
        if api_key:
            headers['Authorization'] = f'Bearer {api_key}'
        req = Request(url, headers=headers, method='GET')
        with urlopen(req, timeout=15.0) as resp:
            payload = json.loads(resp.read().decode('utf-8'))
        rows = payload.get('items', payload.get('results', [])) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            rows = []
        out: list[dict[str, Any]] = []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                continue
            out.append(
                {
                    'title': str(row.get('title', '')),
                    'snippet': str(row.get('snippet', row.get('content', ''))),
                    'url': str(row.get('url', row.get('link', ''))),
                }
            )
        return out

    def _model_probability(
        self,
        *,
        question: str,
        market_price: float,
        provider: Any,
        prompt: str,
        news_rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        endpoint = str(getattr(provider, 'endpoint', '')).strip()
        adapter = str(getattr(provider, 'adapter', '')).strip().lower()
        if adapter != 'openai_compatible' or not endpoint:
            return {}

        news_lines = '\n'.join(
            [f"- {x.get('title', '')}: {x.get('snippet', '')}" for x in news_rows[:5]]
        )
        system = (
            '你是预测建模助手。请仅输出 JSON。'
            '格式: {"probability":0-1,"confidence":0-1,"reason":"简短中文"}。'
        )
        user = (
            f'市场问题: {question}\n'
            f'市场当前Yes价格(近似概率): {market_price:.4f}\n'
            f'相关新闻:\n{news_lines if news_lines else "无外部新闻，按先验判断"}\n'
            f'补充约束: {prompt or "稳健、保守、避免过拟合"}\n'
            '请给出概率与置信度。'
        )
        payload = {
            'model': str(getattr(provider, 'model', '') or 'auto'),
            'temperature': 0.2,
            'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
        }
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }
        key = str(getattr(provider, 'api_key', '') or '').strip()
        if key:
            headers['Authorization'] = f'Bearer {key}'
        extra_headers = normalize_extra_headers(getattr(provider, 'extra_headers', {}) or {})
        headers.update(extra_headers)
        req = Request(endpoint, data=json.dumps(payload).encode('utf-8'), headers=headers, method='POST')
        with urlopen(req, timeout=30.0) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        choices = data.get('choices', []) if isinstance(data, dict) else []
        content = ''
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            msg = choices[0].get('message', {})
            if isinstance(msg, dict):
                content = str(msg.get('content', ''))
        obj = _json_block(content)
        p = _safe_float(obj.get('probability'), -1.0)
        c = _safe_float(obj.get('confidence'), -1.0)
        reason = str(obj.get('reason', '')).strip()
        if not (0.0 <= p <= 1.0 and 0.0 <= c <= 1.0):
            return {}
        return {'probability': p, 'confidence': c, 'reason': reason}

    def llm_health_check(self, provider_id: str = '') -> dict[str, Any]:
        started = time.perf_counter()
        cfg: ModelAllocation = self.router_store.load()
        provider = choose_provider(cfg, provider_id=provider_id)
        if provider is None:
            return {
                'ok': False,
                'status': 'not_configured',
                'provider_id': '',
                'error': '未配置可用模型 Provider',
                'latency_ms': 0,
                'checked_at_utc': _now_utc(),
            }
        endpoint = str(getattr(provider, 'endpoint', '')).strip()
        adapter = str(getattr(provider, 'adapter', '')).strip().lower()
        if adapter != 'openai_compatible' or not endpoint:
            return {
                'ok': False,
                'status': 'unsupported',
                'provider_id': str(getattr(provider, 'provider_id', '')),
                'error': f'当前 adapter={adapter} 仅支持 openai_compatible 健康检查',
                'latency_ms': int((time.perf_counter() - started) * 1000),
                'checked_at_utc': _now_utc(),
            }
        payload = {
            'model': str(getattr(provider, 'model', '') or 'auto'),
            'temperature': 0.0,
            'max_tokens': 16,
            'messages': [
                {'role': 'system', 'content': '仅返回 JSON: {"ok":true}'},
                {'role': 'user', 'content': 'health check'},
            ],
        }
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
        key = str(getattr(provider, 'api_key', '') or '').strip()
        if key:
            headers['Authorization'] = f'Bearer {key}'
        headers.update(normalize_extra_headers(getattr(provider, 'extra_headers', {}) or {}))
        try:
            req = Request(endpoint, data=json.dumps(payload).encode('utf-8'), headers=headers, method='POST')
            with urlopen(req, timeout=20.0) as resp:
                body = json.loads(resp.read().decode('utf-8'))
            choices = body.get('choices', []) if isinstance(body, dict) else []
            content = ''
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                msg = choices[0].get('message', {})
                if isinstance(msg, dict):
                    content = str(msg.get('content', ''))
            if not content and not choices:
                raise RuntimeError('LLM 返回为空')
            return {
                'ok': True,
                'status': 'ok',
                'provider_id': str(getattr(provider, 'provider_id', '')),
                'company': str(getattr(provider, 'company', '')),
                'model': str(getattr(provider, 'model', '')),
                'latency_ms': int((time.perf_counter() - started) * 1000),
                'detail': 'LLM 健康检查通过',
                'checked_at_utc': _now_utc(),
            }
        except Exception as exc:
            return {
                'ok': False,
                'status': 'error',
                'provider_id': str(getattr(provider, 'provider_id', '')),
                'company': str(getattr(provider, 'company', '')),
                'model': str(getattr(provider, 'model', '')),
                'latency_ms': int((time.perf_counter() - started) * 1000),
                'error': str(exc),
                'checked_at_utc': _now_utc(),
            }

    def _ai_signals(
        self,
        markets: list[dict[str, Any]],
        *,
        books: dict[str, dict[str, Any]],
        provider_id: str,
        prompt: str,
    ) -> list[QuantSignal]:
        cfg: ModelAllocation = self.router_store.load()
        providers = self._resolve_ai_providers(cfg, provider_id=provider_id)
        if not providers:
            self.db.insert_event(
                'ai_provider_unavailable',
                'AI概率策略未找到可用 LLM Provider',
                {'provider_id': str(provider_id or ''), 'enabled_count': 0},
            )
            return []
        provider_ids = [str(getattr(x, 'provider_id', '')).strip() for x in providers]

        active_rows: list[dict[str, Any]] = []
        for row in markets:
            if not isinstance(row, dict):
                continue
            closed = bool(row.get('closed', False))
            active = bool(row.get('active', True))
            yes_token = str(row.get('yes_token_id', '')).strip()
            if closed or not active or not yes_token:
                continue
            active_rows.append(row)
        active_rows.sort(key=lambda x: _safe_float(x.get('volume', 0.0)), reverse=True)
        target_count = max(50, int(self.ai_max_markets_per_cycle))
        target_rows = active_rows[:target_count]
        out: list[QuantSignal] = []
        now = _now_utc()
        scanned = 0
        skipped_cached = 0
        evaluated = 0
        triggered_count = 0
        provider_used_counter: dict[str, int] = {}

        for m in target_rows:
            scanned += 1
            market_id = str(m.get('market_id') or m.get('id') or '')
            question = str(m.get('question', '')).strip()
            if not market_id or not question:
                continue

            cached = self.db.ai_eval_recent(market_id, within_sec=self.ai_eval_interval_sec)
            if cached is not None:
                skipped_cached += 1
                continue

            yes_token = str(m.get('yes_token_id', '')).strip()
            no_token = str(m.get('no_token_id', '')).strip()
            yes_mid = _safe_float(m.get('yes_mid', 0.0))
            if yes_mid <= 0:
                yes_mid = _safe_float(m.get('yes_best_ask', _safe_float(m.get('yes_best_bid', 0.0))))
            if yes_mid <= 0 or not yes_token:
                continue

            news_rows: list[dict[str, Any]] = []
            try:
                news_rows = self._fetch_news(query=question, limit=5)
            except Exception:
                news_rows = []

            ai: dict[str, Any] = {}
            used_provider: Any | None = None
            provider_errors: list[dict[str, Any]] = []
            for p in providers:
                pid = str(getattr(p, 'provider_id', '')).strip()
                try:
                    ai = self._model_probability(
                        question=question,
                        market_price=yes_mid,
                        provider=p,
                        prompt=prompt,
                        news_rows=news_rows,
                    )
                    if ai:
                        used_provider = p
                        provider_used_counter[pid] = provider_used_counter.get(pid, 0) + 1
                        break
                    provider_errors.append({'provider_id': pid, 'error': 'empty_or_invalid_response'})
                except Exception as exc:
                    provider_errors.append({'provider_id': pid, 'error': str(exc)})

            if not ai:
                self.db.insert_event(
                    'ai_error',
                    'AI概率评估失败',
                    {
                        'market_id': market_id,
                        'question': question,
                        'provider_errors': provider_errors[:6],
                    },
                )
                continue
            evaluated += 1
            prob = _safe_float(ai.get('probability'), 0.0)
            conf = _safe_float(ai.get('confidence'), 0.0)
            reason = str(ai.get('reason', '')).strip()
            self.db.upsert_ai_eval(
                {
                    'market_id': market_id,
                    'question': question,
                    'probability': prob,
                    'confidence': conf,
                    'model': str(
                        getattr(used_provider, 'model', '')
                        or getattr(used_provider, 'provider_id', '')
                        or ''
                    ),
                    'reason': reason,
                    'news': news_rows,
                    'evaluated_at_utc': now,
                }
            )
            deviation = abs(prob - yes_mid)
            triggered = conf >= self.ai_min_confidence and deviation >= self.ai_deviation_threshold
            self._emit_event(
                'ai_eval',
                f"[AI_EVAL] market={question} yes_mid={yes_mid:.4f} ai_prob={prob:.4f} "
                f"dev={deviation:.4f} conf={conf:.4f} trigger={str(triggered).lower()}",
                {
                    'market_id': market_id,
                    'question': question,
                    'market_yes_mid': yes_mid,
                    'ai_probability': prob,
                    'deviation': deviation,
                    'confidence': conf,
                    'triggered': triggered,
                    'provider_id': str(getattr(used_provider, 'provider_id', '') or ''),
                },
            )
            if not triggered:
                continue
            triggered_count += 1

            side = 'BUY'
            token_id = yes_token
            signal_type = 'ai_yes_buy'
            price = _safe_float(m.get('yes_best_ask', 0.0)) if prob > yes_mid else _safe_float(m.get('no_best_ask', 0.0))
            if prob <= yes_mid:
                token_id = no_token or yes_token
                signal_type = 'ai_no_buy' if no_token else 'ai_yes_sell'
                if not no_token:
                    side = 'SELL'
                    price = _safe_float(m.get('yes_best_bid', 0.0))
            if price <= 0:
                # fallback to mid
                if token_id == yes_token:
                    price = yes_mid
                else:
                    no_mid = _safe_float(m.get('no_mid', 0.0))
                    price = no_mid if no_mid > 0 else max(0.01, 1.0 - yes_mid)

            out.append(
                QuantSignal(
                    time_utc=now,
                    strategy_id='ai_probability',
                    signal_type=signal_type,
                    market_id=market_id,
                    token_id=token_id,
                    side=side,
                    order_kind='limit',
                    price=price,
                    confidence=conf,
                    score=deviation * conf,
                    suggested_notional=25.0,
                    reason={
                        'rule': 'ai_probability_gap',
                        'question': question,
                        'market_yes_mid': yes_mid,
                        'ai_probability': prob,
                        'deviation': deviation,
                        'confidence': conf,
                        'provider_id': str(getattr(used_provider, 'provider_id', '') or ''),
                        'model_reason': reason,
                        'decision_text': f"AI偏差={deviation*100:.2f}% >= 阈值={self.ai_deviation_threshold*100:.2f}%, 置信度={conf:.2f}",
                    },
                )
            )
        self.db.insert_event(
            'ai_scan_summary',
            (
                f"[AI_SCAN] active={len(active_rows)} target={len(target_rows)} scanned={scanned} "
                f"cached_skip={skipped_cached} evaluated={evaluated} triggered={triggered_count} "
                f"providers={','.join([x for x in provider_ids if x])}"
            ),
            {
                'active_markets': len(active_rows),
                'target_markets': len(target_rows),
                'scanned_markets': scanned,
                'cached_skipped': skipped_cached,
                'evaluated_markets': evaluated,
                'triggered_signals': triggered_count,
                'providers': provider_ids,
                'provider_usage': provider_used_counter,
                'ai_eval_interval_sec': int(self.ai_eval_interval_sec),
                'ai_max_markets_per_cycle': int(self.ai_max_markets_per_cycle),
            },
        )
        return out
