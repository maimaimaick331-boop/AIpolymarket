from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
import json
import threading
import time

from libs.quant.signal_engine import execute_workshop_strategy
from libs.services.live_strategy_service import LiveStrategyStore, StrategyConfig


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_utc(v: Any) -> datetime | None:
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


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _parse_levels(book: dict[str, Any], side: str) -> list[tuple[float, float]]:
    key = 'asks' if side == 'buy' else 'bids'
    rows = book.get(key, [])
    out: list[tuple[float, float]] = []
    if not isinstance(rows, list):
        return out

    for row in rows:
        if not isinstance(row, dict):
            continue
        price = _safe_float(row.get('price'))
        size = _safe_float(row.get('size'))
        if price <= 0 or size <= 0:
            continue
        out.append((price, size))

    if side == 'buy':
        out.sort(key=lambda x: x[0])
    else:
        out.sort(key=lambda x: x[0], reverse=True)
    return out


def best_bid_ask(book: dict[str, Any]) -> tuple[float | None, float | None]:
    bids = _parse_levels(book, side='sell')
    asks = _parse_levels(book, side='buy')
    return (bids[0][0] if bids else None, asks[0][0] if asks else None)


@dataclass
class PaperBotStatus:
    running: bool
    token_id: str
    interval_sec: int
    tick: int
    prefer_stream: bool
    stream_events: int
    stream_book_age_sec: float | None


class PaperTradingEngine:
    def __init__(
        self,
        store_dir: Path,
        initial_cash_per_strategy: float,
        fee_bps: float,
        max_order_notional: float,
        log_hook: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.store_dir = store_dir
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.store_dir / 'paper_trading_state.json'
        self.initial_cash_per_strategy = max(1.0, float(initial_cash_per_strategy))
        self.fee_bps = max(0.0, float(fee_bps))
        self.max_order_notional = max(1.0, float(max_order_notional))
        self.log_hook = log_hook
        self._lock = threading.Lock()
        self._state = self._load_or_init()

    def _emit_log(self, payload: dict[str, Any]) -> None:
        if self.log_hook is None:
            return
        try:
            self.log_hook(payload)
        except Exception:
            pass

    def _new_account(self, strategy_id: str) -> dict[str, Any]:
        now = _now()
        return {
            'strategy_id': strategy_id,
            'initial_cash': self.initial_cash_per_strategy,
            'cash': self.initial_cash_per_strategy,
            'positions': {},
            'avg_cost': {},
            'realized_pnl': 0.0,
            'total_fees': 0.0,
            'turnover': 0.0,
            'trade_count': 0,
            'wins': 0,
            'losses': 0,
            'created_at_utc': now,
            'updated_at_utc': now,
        }

    def _empty_state(self) -> dict[str, Any]:
        now = _now()
        return {
            'version': 2,
            'created_at_utc': now,
            'updated_at_utc': now,
            'initial_cash_per_strategy': self.initial_cash_per_strategy,
            'fee_bps': self.fee_bps,
            'max_order_notional': self.max_order_notional,
            'order_seq': 0,
            'fill_seq': 0,
            'accounts': {},
            'orders': [],
            'fills': [],
            'marks': {},
            'token_rules': {},
        }

    def _load_or_init(self) -> dict[str, Any]:
        if not self.path.exists():
            state = self._empty_state()
            self.path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
            return state
        try:
            payload = json.loads(self.path.read_text(encoding='utf-8'))
            if isinstance(payload, dict):
                payload.setdefault('accounts', {})
                payload.setdefault('orders', [])
                payload.setdefault('fills', [])
                payload.setdefault('marks', {})
                payload.setdefault('token_rules', {})
                payload.setdefault('order_seq', 0)
                payload.setdefault('fill_seq', 0)
                payload.setdefault('initial_cash_per_strategy', self.initial_cash_per_strategy)
                payload.setdefault('fee_bps', self.fee_bps)
                payload.setdefault('max_order_notional', self.max_order_notional)
                return payload
        except Exception:
            pass
        return self._empty_state()

    def _save(self) -> None:
        self._state['updated_at_utc'] = _now()
        self.path.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding='utf-8')

    def _account(self, strategy_id: str) -> dict[str, Any]:
        sid = (strategy_id or 'manual').strip() or 'manual'
        accounts = self._state['accounts']
        if sid not in accounts or not isinstance(accounts[sid], dict):
            accounts[sid] = self._new_account(sid)
        return accounts[sid]

    @staticmethod
    def _normalize_tif(order_type: str, source: str = '') -> str:
        v = str(order_type or '').strip().upper()
        if v in {'', 'LIMIT'}:
            return 'GTC'
        if v == 'BOT':
            return 'GTC'
        if v in {'GTC', 'FOK', 'FAK', 'GTD'}:
            return v
        raise ValueError(f'order_type 仅支持 GTC/FOK/FAK/GTD，收到 {order_type}')

    @staticmethod
    def _normalize_market_tif(order_type: str) -> str:
        v = str(order_type or '').strip().upper()
        if v in {'', 'MARKET'}:
            return 'FOK'
        if v in {'FOK', 'FAK'}:
            return v
        raise ValueError(f'market order_type 仅支持 FOK/FAK，收到 {order_type}')

    @staticmethod
    def _is_tick_aligned(price: float, tick_size: float) -> bool:
        if tick_size <= 0:
            return True
        units = price / tick_size
        return abs(units - round(units)) <= 1e-8

    def update_token_rule(
        self,
        token_id: str,
        *,
        tick_size: float | None = None,
        min_size: float | None = None,
        fees_enabled: bool | None = None,
        fee_type: str | None = None,
        fee_bps: float | None = None,
    ) -> dict[str, Any]:
        tid = str(token_id or '').strip()
        if not tid:
            return {}
        with self._lock:
            rules = self._state.setdefault('token_rules', {})
            row = rules.get(tid, {}) if isinstance(rules.get(tid, {}), dict) else {}
            if tick_size is not None:
                row['tick_size'] = max(0.0, float(tick_size))
            if min_size is not None:
                row['min_size'] = max(0.0, float(min_size))
            if fees_enabled is not None:
                row['fees_enabled'] = bool(fees_enabled)
            if fee_type is not None:
                row['fee_type'] = str(fee_type)
            if fee_bps is not None:
                row['fee_bps'] = max(0.0, float(fee_bps))
            row['updated_at_utc'] = _now()
            rules[tid] = row
            self._save()
            return dict(row)

    def update_token_rules_bulk(self, updates: list[dict[str, Any]]) -> int:
        changed = 0
        with self._lock:
            rules = self._state.setdefault('token_rules', {})
            if not isinstance(rules, dict):
                rules = {}
                self._state['token_rules'] = rules
            for up in updates:
                if not isinstance(up, dict):
                    continue
                tid = str(up.get('token_id', '')).strip()
                if not tid:
                    continue
                row = rules.get(tid, {}) if isinstance(rules.get(tid, {}), dict) else {}
                prev = dict(row)
                if up.get('tick_size') is not None:
                    row['tick_size'] = max(0.0, float(up.get('tick_size')))
                if up.get('min_size') is not None:
                    row['min_size'] = max(0.0, float(up.get('min_size')))
                if up.get('fees_enabled') is not None:
                    row['fees_enabled'] = bool(up.get('fees_enabled'))
                if up.get('fee_type') is not None:
                    row['fee_type'] = str(up.get('fee_type'))
                if up.get('fee_bps') is not None:
                    row['fee_bps'] = max(0.0, float(up.get('fee_bps')))
                if row != prev:
                    row['updated_at_utc'] = _now()
                    changed += 1
                rules[tid] = row
            if changed > 0:
                self._save()
        return changed

    def token_rule(self, token_id: str) -> dict[str, Any]:
        tid = str(token_id or '').strip()
        if not tid:
            return {}
        with self._lock:
            rules = self._state.get('token_rules', {})
            row = rules.get(tid, {}) if isinstance(rules, dict) else {}
            if isinstance(row, dict):
                return dict(row)
            return {}

    def list_token_rules(self) -> dict[str, Any]:
        with self._lock:
            rules = self._state.get('token_rules', {})
            if not isinstance(rules, dict):
                return {}
            return {str(k): dict(v) for k, v in rules.items() if isinstance(v, dict)}

    def _next_order_id(self) -> str:
        self._state['order_seq'] = int(self._state.get('order_seq', 0)) + 1
        return f"pord-{self._state['order_seq']:06d}"

    def _next_fill_id(self) -> str:
        self._state['fill_seq'] = int(self._state.get('fill_seq', 0)) + 1
        return f"pfill-{self._state['fill_seq']:06d}"

    def _token_rule_locked(self, token_id: str) -> dict[str, Any]:
        rules = self._state.get('token_rules', {})
        if not isinstance(rules, dict):
            return {}
        row = rules.get(str(token_id or ''), {})
        if isinstance(row, dict):
            return row
        return {}

    def _effective_fee_bps(self, token_id: str, override_fee_bps: float | None = None) -> float:
        if override_fee_bps is not None:
            return max(0.0, float(override_fee_bps))
        rule = self._token_rule_locked(token_id)
        if rule:
            if rule.get('fees_enabled') is False:
                return 0.0
            rb = _safe_float(rule.get('fee_bps'), -1.0)
            if rb >= 0.0:
                return rb
        return max(0.0, float(self.fee_bps))

    def _expire_orders_locked(self) -> int:
        now = _now_dt()
        changed = 0
        for order in self._state.get('orders', []):
            if not isinstance(order, dict):
                continue
            if str(order.get('status', '')) not in {'open', 'partial'}:
                continue
            exp = _parse_iso_utc(order.get('expires_at_utc', ''))
            if exp is None:
                continue
            if now >= exp:
                order['status'] = 'expired'
                order['cancel_reason'] = 'GTD_EXPIRED'
                order['updated_at_utc'] = _now()
                changed += 1
                self._emit_log(
                    {
                        'kind': 'paper_order_expired',
                        'order_id': order.get('order_id', ''),
                        'strategy_id': order.get('strategy_id', ''),
                        'token_id': order.get('token_id', ''),
                    }
                )
        return changed

    def _estimate_limit_fillable_qty(
        self,
        *,
        account: dict[str, Any],
        token_id: str,
        side: str,
        limit_price: float,
        target_qty: float,
        fee_bps: float,
        book: dict[str, Any],
    ) -> float:
        remaining = max(0.0, float(target_qty))
        if remaining <= 1e-12:
            return 0.0
        levels = _parse_levels(book, side=side)
        total = 0.0
        for price, size in levels:
            if remaining <= 1e-12:
                break
            crosses = price <= limit_price if side == 'buy' else price >= limit_price
            if not crosses:
                break
            qty = min(size, remaining)
            qty = self._max_fill_qty(account, token_id, side, qty, price, fee_bps=fee_bps)
            if qty <= 1e-12:
                break
            total += qty
            remaining -= qty
        return max(0.0, total)

    def _estimate_market_fillable_notional_and_qty(
        self,
        *,
        account: dict[str, Any],
        token_id: str,
        side: str,
        amount: float,
        fee_bps: float,
        book: dict[str, Any],
    ) -> tuple[float, float]:
        remaining_notional = max(0.0, float(amount))
        if remaining_notional <= 1e-12:
            return 0.0, 0.0
        levels = _parse_levels(book, side=side)
        total_notional = 0.0
        total_qty = 0.0
        for price, size in levels:
            if remaining_notional <= 1e-12:
                break
            max_qty_by_notional = remaining_notional / price
            qty = min(size, max_qty_by_notional)
            qty = self._max_fill_qty(account, token_id, side, qty, price, fee_bps=fee_bps)
            if qty <= 1e-12:
                break
            notional = qty * price
            total_notional += notional
            total_qty += qty
            remaining_notional -= notional
        return total_notional, total_qty

    def reset(self, initial_cash_per_strategy: float | None = None) -> dict[str, Any]:
        with self._lock:
            if initial_cash_per_strategy is not None:
                self.initial_cash_per_strategy = max(1.0, float(initial_cash_per_strategy))
            rules = self._state.get('token_rules', {})
            rules_copy = {str(k): dict(v) for k, v in (rules or {}).items() if isinstance(v, dict)}
            self._state = self._empty_state()
            self._state['token_rules'] = rules_copy
            self._save()
        return self.status(limit=50)

    def _mark_price(self, token_id: str, book: dict[str, Any]) -> None:
        bid, ask = best_bid_ask(book)
        mark: float | None = None
        if bid is not None and ask is not None:
            mark = (bid + ask) / 2.0
        elif bid is not None:
            mark = bid
        elif ask is not None:
            mark = ask
        if mark is not None and mark > 0:
            self._state['marks'][token_id] = float(mark)

    def _account_position(self, account: dict[str, Any], token_id: str) -> float:
        positions = account.get('positions', {})
        return _safe_float(positions.get(token_id, 0.0))

    def strategy_position(self, strategy_id: str, token_id: str) -> float:
        with self._lock:
            account = self._account(strategy_id)
            return self._account_position(account, token_id)

    def _max_fill_qty(
        self,
        account: dict[str, Any],
        token_id: str,
        side: str,
        qty: float,
        price: float,
        *,
        fee_bps: float,
    ) -> float:
        if qty <= 0 or price <= 0:
            return 0.0
        fee_rate = max(0.0, float(fee_bps)) / 10000.0
        if side == 'buy':
            cash = _safe_float(account.get('cash', 0.0))
            affordable = cash / (price * (1.0 + fee_rate))
            return max(0.0, min(qty, affordable))
        pos = max(0.0, self._account_position(account, token_id))
        return max(0.0, min(qty, pos))

    def _apply_fill(self, order: dict[str, Any], qty: float, price: float, source: str) -> dict[str, Any]:
        side = str(order.get('side', '')).lower()
        if side not in {'buy', 'sell'}:
            raise ValueError('side 必须是 buy 或 sell')

        token_id = str(order.get('token_id', ''))
        strategy_id = str(order.get('strategy_id', 'manual'))
        account = self._account(strategy_id)

        fee_bps = _safe_float(order.get('fee_bps', self.fee_bps), self.fee_bps)
        fee_rate = max(0.0, fee_bps) / 10000.0
        notional = qty * price
        fee = notional * fee_rate

        pos = self._account_position(account, token_id)
        avg_cost = _safe_float(account.get('avg_cost', {}).get(token_id, 0.0))
        positions = account.setdefault('positions', {})
        avg_map = account.setdefault('avg_cost', {})

        if side == 'buy':
            account['cash'] = _safe_float(account.get('cash', 0.0)) - notional - fee
            new_pos = pos + qty
            if new_pos > 1e-12:
                avg_map[token_id] = (avg_cost * pos + notional) / new_pos
            else:
                avg_map[token_id] = 0.0
            positions[token_id] = new_pos
            realized = 0.0
        else:
            sell_qty = min(qty, max(0.0, pos))
            notional = sell_qty * price
            fee = notional * fee_rate
            realized = (price - avg_cost) * sell_qty
            account['cash'] = _safe_float(account.get('cash', 0.0)) + notional - fee
            new_pos = pos - sell_qty
            if new_pos <= 1e-12:
                positions[token_id] = 0.0
                avg_map[token_id] = 0.0
            else:
                positions[token_id] = new_pos

            account['realized_pnl'] = _safe_float(account.get('realized_pnl', 0.0)) + realized
            if sell_qty > 1e-12:
                if realized > 1e-12:
                    account['wins'] = int(account.get('wins', 0)) + 1
                elif realized < -1e-12:
                    account['losses'] = int(account.get('losses', 0)) + 1

        account['total_fees'] = _safe_float(account.get('total_fees', 0.0)) + fee
        account['turnover'] = _safe_float(account.get('turnover', 0.0)) + notional
        account['trade_count'] = int(account.get('trade_count', 0)) + 1
        account['updated_at_utc'] = _now()

        fill = {
            'fill_id': self._next_fill_id(),
            'order_id': str(order.get('order_id', '')),
            'strategy_id': strategy_id,
            'token_id': token_id,
            'side': side,
            'quantity': qty,
            'price': price,
            'notional': notional,
            'fee': fee,
            'fee_bps': fee_bps,
            'source': source,
            'time_utc': _now(),
        }
        self._state['fills'].append(fill)
        if len(self._state['fills']) > 10000:
            self._state['fills'] = self._state['fills'][-10000:]

        self._emit_log(
            {
                'kind': 'paper_fill',
                'fill_id': fill['fill_id'],
                'order_id': fill['order_id'],
                'strategy_id': strategy_id,
                'token_id': token_id,
                'side': side,
                'price': price,
                'size': qty,
                'fee': fee,
                'source': source,
            }
        )
        return fill

    def _match_limit_order(self, order: dict[str, Any], book: dict[str, Any], source: str) -> list[dict[str, Any]]:
        side = str(order.get('side', '')).lower()
        token_id = str(order.get('token_id', ''))
        limit_price = _safe_float(order.get('limit_price', 0.0))
        remaining = _safe_float(order.get('remaining', 0.0))
        if side not in {'buy', 'sell'} or remaining <= 1e-12:
            return []

        levels = _parse_levels(book, side=side)
        fills: list[dict[str, Any]] = []
        account = self._account(str(order.get('strategy_id', 'manual')))
        fee_bps = _safe_float(order.get('fee_bps', self.fee_bps), self.fee_bps)

        for price, size in levels:
            if remaining <= 1e-12:
                break
            crosses = price <= limit_price if side == 'buy' else price >= limit_price
            if not crosses:
                break
            qty = min(remaining, size)
            qty = self._max_fill_qty(account, token_id, side, qty, price, fee_bps=fee_bps)
            if qty <= 1e-12:
                break
            fill = self._apply_fill(order=order, qty=qty, price=price, source=source)
            fills.append(fill)
            remaining -= qty

        order['remaining'] = max(0.0, remaining)
        order['filled_qty'] = max(0.0, _safe_float(order.get('quantity', 0.0)) - order['remaining'])
        order['fill_count'] = int(order.get('fill_count', 0)) + len(fills)
        order['filled_notional'] = _safe_float(order.get('filled_notional', 0.0)) + sum(
            _safe_float(x.get('notional', 0.0)) for x in fills
        )
        if order['remaining'] <= 1e-12:
            order['remaining'] = 0.0
            order['status'] = 'filled'
        elif order['remaining'] < _safe_float(order.get('quantity', 0.0)) - 1e-12:
            order['status'] = 'partial'
        else:
            order['status'] = 'open'
        order['updated_at_utc'] = _now()
        return fills

    def on_book(self, token_id: str, book: dict[str, Any], source: str = 'tick') -> dict[str, Any]:
        with self._lock:
            self._mark_price(token_id=token_id, book=book)
            self._expire_orders_locked()
            matched = 0
            for order in self._state['orders']:
                if not isinstance(order, dict):
                    continue
                if str(order.get('token_id', '')) != token_id:
                    continue
                if str(order.get('status', '')) not in {'open', 'partial'}:
                    continue
                if str(order.get('order_kind', 'limit')) != 'limit':
                    continue
                fills = self._match_limit_order(order=order, book=book, source=source)
                matched += len(fills)
            self._save()
            return {'matched': matched}

    def place_limit_order(
        self,
        strategy_id: str,
        token_id: str,
        side: str,
        price: float,
        size: float,
        order_type: str = 'GTC',
        source: str = 'manual',
        book: dict[str, Any] | None = None,
        expire_seconds: int | None = None,
        tick_size: float | None = None,
        min_size: float | None = None,
        fee_bps: float | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._expire_orders_locked()
            side = side.lower().strip()
            if side not in {'buy', 'sell'}:
                raise ValueError('side 仅支持 buy/sell')
            tif = self._normalize_tif(order_type=order_type, source=source)
            if price <= 0 or size <= 0:
                raise ValueError('price 和 size 必须大于 0')

            rule = self._token_rule_locked(token_id)
            effective_tick = max(
                0.0,
                _safe_float(
                    tick_size if tick_size is not None else rule.get('tick_size', 0.0),
                    0.0,
                ),
            )
            effective_min_size = max(
                0.0,
                _safe_float(
                    min_size if min_size is not None else rule.get('min_size', 0.0),
                    0.0,
                ),
            )
            if effective_tick > 0 and not self._is_tick_aligned(price=price, tick_size=effective_tick):
                raise ValueError(f'price={price} 未对齐 tick_size={effective_tick}')
            if effective_min_size > 0 and size < effective_min_size:
                raise ValueError(f'size={size} 小于最小下单量 min_size={effective_min_size}')

            notional = price * size
            if notional > self.max_order_notional:
                raise ValueError(f'单笔名义金额 {notional:.4f} 超过上限 {self.max_order_notional:.4f}')

            sid = (strategy_id or 'manual').strip() or 'manual'
            account = self._account(sid)
            effective_fee_bps = self._effective_fee_bps(token_id=token_id, override_fee_bps=fee_bps)
            affordable = self._max_fill_qty(
                account,
                token_id,
                side,
                size,
                price,
                fee_bps=effective_fee_bps,
            )
            if affordable + 1e-9 < size:
                raise ValueError('资金/仓位不足，无法下单指定数量')
            if tif in {'FOK', 'FAK'} and not isinstance(book, dict):
                raise ValueError(f'{tif} 下单需要最新 orderbook')

            now = _now()
            expires_at_utc = ''
            if tif == 'GTD':
                ttl_sec = max(1, int(expire_seconds or 300))
                expires_at_utc = (_now_dt() + timedelta(seconds=ttl_sec)).isoformat()

            order = {
                'order_id': self._next_order_id(),
                'strategy_id': sid,
                'token_id': token_id,
                'side': side,
                'order_kind': 'limit',
                'order_type': tif,
                'time_in_force': tif,
                'quantity': float(size),
                'requested_quantity': float(size),
                'limit_price': float(price),
                'remaining': float(size),
                'filled_qty': 0.0,
                'filled_notional': 0.0,
                'fill_count': 0,
                'fee_bps': float(effective_fee_bps),
                'tick_size': effective_tick,
                'min_size': effective_min_size,
                'fees_enabled': rule.get('fees_enabled') if isinstance(rule, dict) else None,
                'fee_type': str(rule.get('fee_type', '')) if isinstance(rule, dict) else '',
                'expires_at_utc': expires_at_utc,
                'status': 'open',
                'source': source,
                'created_at_utc': now,
                'updated_at_utc': now,
            }
            self._state['orders'].append(order)
            if len(self._state['orders']) > 5000:
                self._state['orders'] = self._state['orders'][-5000:]

            self._emit_log(
                {
                    'kind': 'paper_order',
                    'order_id': order['order_id'],
                    'strategy_id': sid,
                    'token_id': token_id,
                    'side': side,
                    'price': price,
                    'size': size,
                    'order_type': tif,
                    'time_in_force': tif,
                    'tick_size': effective_tick,
                    'min_size': effective_min_size,
                    'fee_bps': effective_fee_bps,
                    'expires_at_utc': expires_at_utc,
                    'source': source,
                }
            )

            fills: list[dict[str, Any]] = []
            if isinstance(book, dict):
                self._mark_price(token_id=token_id, book=book)
                if tif == 'FOK':
                    fillable = self._estimate_limit_fillable_qty(
                        account=account,
                        token_id=token_id,
                        side=side,
                        limit_price=price,
                        target_qty=size,
                        fee_bps=effective_fee_bps,
                        book=book,
                    )
                    if fillable + 1e-9 < size:
                        order['status'] = 'cancelled'
                        order['cancel_reason'] = 'FOK_NOT_FILLABLE'
                        order['updated_at_utc'] = _now()
                    else:
                        fills = self._match_limit_order(order=order, book=book, source=source)
                else:
                    fills = self._match_limit_order(order=order, book=book, source=source)
                    if tif == 'FAK' and order.get('status') in {'open', 'partial'}:
                        order['status'] = 'cancelled'
                        order['cancel_reason'] = 'FAK_UNFILLED_CANCELLED' if fills else 'FAK_NO_LIQUIDITY'
                        order['updated_at_utc'] = _now()
            elif tif in {'FOK', 'FAK'}:
                order['status'] = 'cancelled'
                order['cancel_reason'] = 'NO_ORDERBOOK'
                order['updated_at_utc'] = _now()
            self._save()
            return {'order': order, 'fills': fills}

    def place_market_order(
        self,
        strategy_id: str,
        token_id: str,
        side: str,
        amount: float,
        order_type: str = 'FOK',
        source: str = 'manual',
        book: dict[str, Any] | None = None,
        fee_bps: float | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._expire_orders_locked()
            side = side.lower().strip()
            if side not in {'buy', 'sell'}:
                raise ValueError('side 仅支持 buy/sell')
            tif = self._normalize_market_tif(order_type)
            if amount <= 0:
                raise ValueError('amount 必须大于 0')
            if amount > self.max_order_notional:
                raise ValueError(f'单笔金额 {amount:.4f} 超过上限 {self.max_order_notional:.4f}')
            if not isinstance(book, dict):
                raise ValueError('market 下单需要最新 orderbook')

            sid = (strategy_id or 'manual').strip() or 'manual'
            levels = _parse_levels(book, side=side)
            if not levels:
                raise ValueError('当前无可成交盘口')

            rule = self._token_rule_locked(token_id)
            effective_fee_bps = self._effective_fee_bps(token_id=token_id, override_fee_bps=fee_bps)
            now = _now()
            order = {
                'order_id': self._next_order_id(),
                'strategy_id': sid,
                'token_id': token_id,
                'side': side,
                'order_kind': 'market',
                'order_type': tif,
                'time_in_force': tif,
                'quantity': 0.0,
                'limit_price': levels[0][0],
                'remaining': 0.0,
                'request_notional': amount,
                'filled_notional': 0.0,
                'remaining_notional': amount,
                'filled_qty': 0.0,
                'fill_count': 0,
                'fee_bps': float(effective_fee_bps),
                'fees_enabled': rule.get('fees_enabled') if isinstance(rule, dict) else None,
                'fee_type': str(rule.get('fee_type', '')) if isinstance(rule, dict) else '',
                'status': 'open',
                'source': source,
                'created_at_utc': now,
                'updated_at_utc': now,
            }
            self._state['orders'].append(order)
            if len(self._state['orders']) > 5000:
                self._state['orders'] = self._state['orders'][-5000:]

            remaining_notional = float(amount)
            fills: list[dict[str, Any]] = []
            account = self._account(sid)

            self._mark_price(token_id=token_id, book=book)
            if tif == 'FOK':
                fillable_notional, _ = self._estimate_market_fillable_notional_and_qty(
                    account=account,
                    token_id=token_id,
                    side=side,
                    amount=amount,
                    fee_bps=effective_fee_bps,
                    book=book,
                )
                if fillable_notional + 1e-6 < amount:
                    order['status'] = 'cancelled'
                    order['cancel_reason'] = 'FOK_NOT_FILLABLE'
                    order['updated_at_utc'] = _now()
                    self._save()
                    self._emit_log(
                        {
                            'kind': 'paper_order',
                            'order_id': order['order_id'],
                            'strategy_id': sid,
                            'token_id': token_id,
                            'side': side,
                            'amount': amount,
                            'order_type': tif,
                            'source': source,
                            'status': order['status'],
                            'cancel_reason': order['cancel_reason'],
                        }
                    )
                    return {'order': order, 'fills': fills}

            for price, size in levels:
                if remaining_notional <= 1e-12:
                    break
                max_qty_by_notional = remaining_notional / price
                qty = min(size, max_qty_by_notional)
                qty = self._max_fill_qty(account, token_id, side, qty, price, fee_bps=effective_fee_bps)
                if qty <= 1e-12:
                    break
                fill = self._apply_fill(order=order, qty=qty, price=price, source=source)
                fills.append(fill)
                notional = qty * price
                order['quantity'] = _safe_float(order.get('quantity', 0.0)) + qty
                order['filled_notional'] = _safe_float(order.get('filled_notional', 0.0)) + notional
                remaining_notional -= notional

            order['remaining_notional'] = max(0.0, remaining_notional)
            order['filled_qty'] = _safe_float(order.get('quantity', 0.0))
            order['fill_count'] = len(fills)

            if fills and remaining_notional <= 1e-6:
                order['status'] = 'filled'
            elif fills:
                if tif == 'FAK':
                    order['status'] = 'cancelled'
                    order['cancel_reason'] = 'FAK_UNFILLED_CANCELLED'
                else:
                    order['status'] = 'partial'
            else:
                order['status'] = 'cancelled'
                if tif == 'FAK':
                    order['cancel_reason'] = 'FAK_NO_LIQUIDITY'
            order['updated_at_utc'] = _now()
            self._save()

            self._emit_log(
                {
                    'kind': 'paper_order',
                    'order_id': order['order_id'],
                    'strategy_id': sid,
                    'token_id': token_id,
                    'side': side,
                    'amount': amount,
                    'order_type': tif,
                    'time_in_force': tif,
                    'fee_bps': effective_fee_bps,
                    'source': source,
                    'status': order['status'],
                    'cancel_reason': order.get('cancel_reason', ''),
                }
            )
            return {'order': order, 'fills': fills}

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        with self._lock:
            for order in self._state['orders']:
                if str(order.get('order_id', '')) != order_id:
                    continue
                if str(order.get('status', '')) in {'open', 'partial'}:
                    order['status'] = 'cancelled'
                    order['updated_at_utc'] = _now()
                    self._save()
                    self._emit_log({'kind': 'paper_cancel_order', 'order_id': order_id})
                    return {'ok': True, 'order': order}
                return {'ok': False, 'order': order, 'reason': '订单不可撤销'}
            return {'ok': False, 'reason': 'order_id 不存在'}

    def cancel_all(self) -> dict[str, Any]:
        with self._lock:
            cancelled = 0
            for order in self._state['orders']:
                if str(order.get('status', '')) in {'open', 'partial'}:
                    order['status'] = 'cancelled'
                    order['updated_at_utc'] = _now()
                    cancelled += 1
            self._save()
            self._emit_log({'kind': 'paper_cancel_all', 'count': cancelled})
            return {'ok': True, 'cancelled': cancelled}

    def list_orders(self, limit: int = 200, strategy_id: str = '', open_only: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            changed = self._expire_orders_locked()
            if changed > 0:
                self._save()
            rows = [x for x in self._state['orders'] if isinstance(x, dict)]
            if strategy_id:
                rows = [x for x in rows if str(x.get('strategy_id', '')) == strategy_id]
            if open_only:
                rows = [x for x in rows if str(x.get('status', '')) in {'open', 'partial'}]
            rows = rows[-max(1, limit) :]
            return rows

    def list_fills(self, limit: int = 200, strategy_id: str = '') -> list[dict[str, Any]]:
        with self._lock:
            changed = self._expire_orders_locked()
            if changed > 0:
                self._save()
            rows = [x for x in self._state['fills'] if isinstance(x, dict)]
            if strategy_id:
                rows = [x for x in rows if str(x.get('strategy_id', '')) == strategy_id]
            rows = rows[-max(1, limit) :]
            return rows

    def _account_snapshot(self, account: dict[str, Any], open_orders_count: int) -> dict[str, Any]:
        sid = str(account.get('strategy_id', 'manual'))
        cash = _safe_float(account.get('cash', 0.0))
        initial = max(1e-9, _safe_float(account.get('initial_cash', self.initial_cash_per_strategy)))
        positions = account.get('positions', {})
        avg_cost = account.get('avg_cost', {})

        equity = cash
        unrealized = 0.0
        position_rows: list[dict[str, Any]] = []
        for token_id, qty_raw in positions.items():
            qty = _safe_float(qty_raw)
            if abs(qty) <= 1e-12:
                continue
            avg = _safe_float(avg_cost.get(token_id, 0.0))
            mark = _safe_float(self._state.get('marks', {}).get(token_id, avg))
            equity += qty * mark
            unrealized += (mark - avg) * qty
            position_rows.append(
                {
                    'token_id': token_id,
                    'qty': qty,
                    'avg_cost': avg,
                    'mark_price': mark,
                    'unrealized_pnl': (mark - avg) * qty,
                }
            )

        realized = _safe_float(account.get('realized_pnl', 0.0))
        fees = _safe_float(account.get('total_fees', 0.0))
        total_pnl = equity - initial
        wins = int(account.get('wins', 0))
        losses = int(account.get('losses', 0))
        closed = wins + losses
        win_rate = (wins / closed) if closed > 0 else 0.0

        return {
            'strategy_id': sid,
            'initial_cash': initial,
            'cash': cash,
            'equity': equity,
            'total_pnl': total_pnl,
            'total_pnl_pct': total_pnl / initial * 100.0,
            'realized_pnl': realized,
            'unrealized_pnl': unrealized,
            'total_fees': fees,
            'turnover': _safe_float(account.get('turnover', 0.0)),
            'trade_count': int(account.get('trade_count', 0)),
            'wins': wins,
            'losses': losses,
            'win_rate': win_rate,
            'open_orders': open_orders_count,
            'positions': position_rows,
            'updated_at_utc': str(account.get('updated_at_utc', '')),
        }

    def account_snapshot(self, strategy_id: str) -> dict[str, Any]:
        with self._lock:
            changed = self._expire_orders_locked()
            if changed > 0:
                self._save()
            account = self._account(strategy_id)
            open_orders = 0
            for order in self._state['orders']:
                if not isinstance(order, dict):
                    continue
                if str(order.get('strategy_id', '')) != strategy_id:
                    continue
                if str(order.get('status', '')) in {'open', 'partial'}:
                    open_orders += 1
            return self._account_snapshot(account, open_orders_count=open_orders)

    def list_positions(self, strategy_id: str = '') -> list[dict[str, Any]]:
        with self._lock:
            out: list[dict[str, Any]] = []
            accounts = self._state.get('accounts', {})
            for sid, account in accounts.items():
                if strategy_id and sid != strategy_id:
                    continue
                if not isinstance(account, dict):
                    continue
                snap = self._account_snapshot(account, open_orders_count=0)
                for row in snap['positions']:
                    out.append({'strategy_id': sid, **row})
            return out

    def status(self, limit: int = 50) -> dict[str, Any]:
        with self._lock:
            changed = self._expire_orders_locked()
            if changed > 0:
                self._save()
            orders = [x for x in self._state['orders'] if isinstance(x, dict)]
            open_orders = [x for x in orders if str(x.get('status', '')) in {'open', 'partial'}]
            account_open: dict[str, int] = {}
            for order in open_orders:
                sid = str(order.get('strategy_id', 'manual'))
                account_open[sid] = account_open.get(sid, 0) + 1

            accounts = self._state.get('accounts', {})
            snaps: list[dict[str, Any]] = []
            for sid, account in accounts.items():
                if not isinstance(account, dict):
                    continue
                snaps.append(self._account_snapshot(account, open_orders_count=account_open.get(sid, 0)))

            snaps.sort(key=lambda x: x['total_pnl'], reverse=True)
            total_equity = sum(_safe_float(x.get('equity', 0.0)) for x in snaps)
            total_realized = sum(_safe_float(x.get('realized_pnl', 0.0)) for x in snaps)
            total_fees = sum(_safe_float(x.get('total_fees', 0.0)) for x in snaps)
            total_unrealized = sum(_safe_float(x.get('unrealized_pnl', 0.0)) for x in snaps)
            total_initial = sum(_safe_float(x.get('initial_cash', 0.0)) for x in snaps)
            total_pnl = total_equity - total_initial

            return {
                'updated_at_utc': str(self._state.get('updated_at_utc', '')),
                'initial_cash_per_strategy': _safe_float(self._state.get('initial_cash_per_strategy', self.initial_cash_per_strategy)),
                'fee_bps': _safe_float(self._state.get('fee_bps', self.fee_bps)),
                'max_order_notional': _safe_float(self._state.get('max_order_notional', self.max_order_notional)),
                'token_rules_count': len(self._state.get('token_rules', {})) if isinstance(self._state.get('token_rules', {}), dict) else 0,
                'accounts_count': len(snaps),
                'orders_count': len(orders),
                'open_orders_count': len(open_orders),
                'fills_count': len(self._state['fills']),
                'totals': {
                    'initial_cash': total_initial,
                    'equity': total_equity,
                    'total_pnl': total_pnl,
                    'realized_pnl': total_realized,
                    'unrealized_pnl': total_unrealized,
                    'total_fees': total_fees,
                },
                'leaderboard': snaps[: max(1, min(limit, 300))],
            }


class PaperBotManager:
    def __init__(
        self,
        client_factory,
        strategy_store: LiveStrategyStore,
        paper_engine: PaperTradingEngine,
        market_rows_provider: Callable[[], list[dict[str, Any]]] | None = None,
        ai_eval_provider: Callable[[str], dict[str, Any] | None] | None = None,
    ) -> None:
        self.client_factory = client_factory
        self.strategy_store = strategy_store
        self.paper_engine = paper_engine
        self.market_rows_provider = market_rows_provider
        self.ai_eval_provider = ai_eval_provider
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._token_id = ''
        self._interval_sec = 12
        self._tick = 0
        self._prefer_stream = True
        self._stream_books: dict[str, dict[str, Any]] = {}
        self._stream_events = 0
        self._state: dict[str, dict[str, Any]] = {}

    def set_market_data_providers(
        self,
        *,
        market_rows_provider: Callable[[], list[dict[str, Any]]] | None = None,
        ai_eval_provider: Callable[[str], dict[str, Any] | None] | None = None,
    ) -> None:
        if market_rows_provider is not None:
            self.market_rows_provider = market_rows_provider
        if ai_eval_provider is not None:
            self.ai_eval_provider = ai_eval_provider

    def start(self, token_id: str, interval_sec: int = 12, prefer_stream: bool = True) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._token_id = token_id.strip()
            self._interval_sec = max(2, int(interval_sec))
            self._tick = 0
            self._prefer_stream = bool(prefer_stream)
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            self.strategy_store.append_log(
                {
                    'kind': 'paper_bot_start',
                    'token_id': self._token_id,
                    'interval_sec': self._interval_sec,
                    'prefer_stream': self._prefer_stream,
                }
            )

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            t = self._thread
        if t and t.is_alive():
            t.join(timeout=5)
        self.strategy_store.append_log({'kind': 'paper_bot_stop'})

    def ingest_book(self, token_id: str, book: dict[str, Any], source: str = 'stream') -> None:
        token = str(token_id or '').strip()
        if not token or not isinstance(book, dict):
            return
        with self._lock:
            self._stream_books[token] = {
                'book': book,
                'source': source,
                'mono_ts': time.monotonic(),
                'time_utc': _now(),
            }
            self._stream_events += 1

    def status(self) -> PaperBotStatus:
        running = bool(self._thread and self._thread.is_alive() and not self._stop.is_set())
        age_sec: float | None = None
        with self._lock:
            token = self._token_id
            rec = self._stream_books.get(token, {})
            if isinstance(rec, dict) and rec.get('mono_ts') is not None:
                age_sec = max(0.0, time.monotonic() - float(rec.get('mono_ts', 0.0)))
            prefer_stream = self._prefer_stream
            stream_events = self._stream_events
        return PaperBotStatus(
            running=running,
            token_id=token,
            interval_sec=self._interval_sec,
            tick=self._tick,
            prefer_stream=prefer_stream,
            stream_events=stream_events,
            stream_book_age_sec=age_sec,
        )

    def _run(self) -> None:
        while not self._stop.is_set():
            self._tick += 1
            try:
                self._one_tick()
            except Exception as exc:
                self.strategy_store.append_log({'kind': 'paper_bot_error', 'tick': self._tick, 'error': str(exc)})
            time.sleep(self._interval_sec)

    def _signal_for_strategy(
        self,
        s: StrategyConfig,
        st: dict[str, Any],
        mid: float,
        token_id: str,
    ) -> tuple[str | None, str]:
        st['mids'].append(mid)
        if len(st['mids']) > 50:
            st['mids'].pop(0)

        pos = self.paper_engine.strategy_position(s.strategy_id, token_id)
        if s.strategy_type == 'mean_reversion':
            window = max(3, int(s.params.get('mean_rev_window', 8)))
            th = max(0.0001, float(s.params.get('mean_rev_threshold', 0.015)))
            hist = st['mids'][-window:]
            if len(hist) < max(3, window // 2):
                return None, ''
            avg = sum(hist) / len(hist)
            if pos <= 1e-9 and mid < avg * (1.0 - th):
                return 'buy', f"mean_reversion: mid={mid:.4f} < avg={avg:.4f}*(1-{th:.4f})"
            if pos > 1e-9 and mid > avg * (1.0 + th):
                return 'sell', f"mean_reversion: mid={mid:.4f} > avg={avg:.4f}*(1+{th:.4f})"
            return None, ''

        hold_ticks = max(1, int(s.params.get('hold_ticks', 2)))
        if pos <= 1e-9:
            return 'buy', f"periodic: pos={pos:.6f} <= 0, entry signal"
        if self._tick - int(st.get('entry_tick', -1)) >= hold_ticks:
            return 'sell', f"periodic: hold_ticks={hold_ticks} reached, exit signal"
        return None, ''

    def _strategy_interval_ticks(self, s: StrategyConfig) -> int:
        check_minutes = max(1.0, _safe_float(s.params.get('check_interval_minutes', 0.0), 0.0))
        if check_minutes <= 0:
            return 1
        tick_span = int(round((check_minutes * 60.0) / max(1, self._interval_sec)))
        return max(1, tick_span)

    def _effective_order_qty(self, s: StrategyConfig, token_id: str, price: float) -> tuple[float, str]:
        params = s.params if isinstance(s.params, dict) else {}
        qty = max(0.0, _safe_float(params.get('order_qty', 2.0), 2.0))
        note = f"order_qty={qty:.6f}"
        order_notional = _safe_float(params.get('order_notional_usdc', 0.0), 0.0)
        if order_notional > 0 and price > 0:
            qty = max(0.0, order_notional / price)
            note = f"order_notional_usdc={order_notional:.4f} / price={price:.6f} => qty={qty:.6f}"

        allow_min_size_override = bool(params.get('allow_min_size_override', False))
        rule = self.paper_engine.token_rule(token_id)
        min_size = max(0.0, _safe_float(rule.get('min_size', 0.0), 0.0))
        if allow_min_size_override and min_size > 0 and qty > 1e-12 and qty < min_size:
            prev = qty
            qty = min_size
            note += f" | min_size_override {prev:.6f}->{qty:.6f}"
        return qty, note

    @staticmethod
    def _is_workshop_strategy(s: StrategyConfig) -> bool:
        if str(s.strategy_type or '').strip().lower() == 'workshop':
            return True
        params = s.params if isinstance(s.params, dict) else {}
        return isinstance(params.get('workshop_spec'), dict)

    def _get_book_for_token(self, token_id: str, *, default_book: dict[str, Any] | None = None) -> dict[str, Any] | None:
        tid = str(token_id or '').strip()
        if not tid:
            return None
        if isinstance(default_book, dict):
            return default_book
        with self._lock:
            rec = self._stream_books.get(tid, {})
            if isinstance(rec, dict):
                maybe = rec.get('book')
                if isinstance(maybe, dict):
                    return maybe
        try:
            cli = self.client_factory()
            polled = cli.get_orderbook(tid)
            if isinstance(polled, dict):
                return polled
        except Exception:
            return None
        return None

    def _workshop_candidate_markets(
        self,
        s: StrategyConfig,
        *,
        fallback_token_id: str,
        fallback_mid: float,
        fallback_book: dict[str, Any],
    ) -> list[dict[str, Any]]:
        params = s.params if isinstance(s.params, dict) else {}
        spec = params.get('workshop_spec') if isinstance(params.get('workshop_spec'), dict) else {}
        market_filter = spec.get('market_filter') if isinstance(spec.get('market_filter'), dict) else {}

        min_volume = max(
            0.0,
            _safe_float(
                market_filter.get('min_volume_24h', params.get('mm_min_volume', 0.0)),
                0.0,
            ),
        )
        min_liq = max(
            0.0,
            _safe_float(
                market_filter.get('min_liquidity', 0.0),
                0.0,
            ),
        )
        keywords_raw = market_filter.get('keywords', params.get('target_markets', 'all'))
        if isinstance(keywords_raw, str):
            text = keywords_raw.strip()
            if not text or text.lower() == 'all':
                keywords: list[str] = []
            else:
                keywords = [x.strip().lower() for x in text.replace('，', ',').split(',') if x.strip()]
        elif isinstance(keywords_raw, list):
            keywords = [str(x).strip().lower() for x in keywords_raw if str(x).strip()]
        else:
            keywords = []

        rows: list[dict[str, Any]] = []
        provider = self.market_rows_provider
        if provider is not None:
            try:
                src = provider()
                if isinstance(src, list):
                    rows = [x for x in src if isinstance(x, dict)]
            except Exception:
                rows = []

        if not rows:
            bid, ask = best_bid_ask(fallback_book)
            rows = [
                {
                    'market_id': '',
                    'question': fallback_token_id,
                    'liquidity': 0.0,
                    'volume': 0.0,
                    'yes_token_id': fallback_token_id,
                    'no_token_id': '',
                    'yes_best_bid': bid or 0.0,
                    'yes_best_ask': ask or 0.0,
                    'yes_mid': fallback_mid,
                    'yes_spread': max(0.0, (ask or 0.0) - (bid or 0.0)),
                    'no_spread': 0.0,
                    'yes_no_sum': 0.0,
                }
            ]

        out: list[dict[str, Any]] = []
        for row in rows:
            question = str(row.get('question', '')).strip()
            volume = _safe_float(row.get('volume', 0.0))
            liq = _safe_float(row.get('liquidity', 0.0))
            if volume < min_volume:
                continue
            if liq < min_liq:
                continue
            if keywords:
                q_low = question.lower()
                if not any(k in q_low for k in keywords):
                    continue
            out.append(row)
        if not out:
            return []
        out.sort(key=lambda x: (_safe_float(x.get('volume', 0.0)), _safe_float(x.get('liquidity', 0.0))), reverse=True)
        return out[:40]

    def _execute_workshop_strategy(
        self,
        s: StrategyConfig,
        st: dict[str, Any],
        *,
        fallback_token_id: str,
        fallback_mid: float,
        fallback_book: dict[str, Any],
        used_stream: bool,
    ) -> bool:
        params = s.params if isinstance(s.params, dict) else {}
        spec = params.get('workshop_spec') if isinstance(params.get('workshop_spec'), dict) else {}
        if not spec:
            return False

        markets = self._workshop_candidate_markets(
            s,
            fallback_token_id=fallback_token_id,
            fallback_mid=fallback_mid,
            fallback_book=fallback_book,
        )
        if not markets:
            self.strategy_store.append_log(
                {
                    'kind': 'paper_bot_check',
                    'strategy_id': s.strategy_id,
                    'strategy_type': s.strategy_type,
                    'token_id': fallback_token_id,
                    'tick': self._tick,
                    'triggered': False,
                    'decision': 'hold',
                    'reason': 'no_market_match_filter',
                }
            )
            return False

        traded = False
        for market in markets:
            market_id = str(market.get('market_id', '')).strip()
            market_name = str(market.get('question', market_id or fallback_token_id)).strip() or fallback_token_id
            ai_eval = None
            if self.ai_eval_provider is not None and market_id:
                try:
                    ai_eval = self.ai_eval_provider(market_id)
                except Exception:
                    ai_eval = None

            eval_out = execute_workshop_strategy(spec, market, ai_eval=ai_eval)
            checks = eval_out.get('checks', []) if isinstance(eval_out.get('checks'), list) else []
            check_texts: list[str] = []
            for row in checks:
                if not isinstance(row, dict):
                    continue
                check_texts.append(
                    f"{row.get('type', '')}:{_safe_float(row.get('actual', 0.0)):.4f}{row.get('operator', '')}{_safe_float(row.get('threshold', 0.0)):.4f}"
                    f"{'✓' if bool(row.get('passed', False)) else '✗'}"
                )
            trigger_reason = ' | '.join(check_texts) if check_texts else 'no_checks'
            triggered = bool(eval_out.get('triggered', False))
            decision = str(eval_out.get('decision', 'hold')).strip().lower()
            self.strategy_store.append_log(
                {
                    'kind': 'paper_bot_check',
                    'strategy_id': s.strategy_id,
                    'strategy_type': s.strategy_type,
                    'market_id': market_id,
                    'market_name': market_name,
                    'token_id': str(market.get('yes_token_id', '')).strip() or fallback_token_id,
                    'tick': self._tick,
                    'triggered': triggered,
                    'decision': decision,
                    'market_price': _safe_float(eval_out.get('market_price', 0.0)),
                    'ai_probability': _safe_float(eval_out.get('ai_probability', 0.0)),
                    'ai_confidence': _safe_float(eval_out.get('ai_confidence', 0.0)),
                    'deviation': _safe_float(eval_out.get('deviation', 0.0)),
                    'reason': trigger_reason if triggered else f"not_triggered | {trigger_reason}",
                }
            )
            if not triggered or traded:
                continue

            yes_token = str(market.get('yes_token_id', '')).strip()
            no_token = str(market.get('no_token_id', '')).strip()
            token_id = yes_token or fallback_token_id
            side = 'buy'
            if decision == 'buy_no' and no_token:
                token_id = no_token
                side = 'buy'
            elif decision == 'buy_yes':
                token_id = yes_token or fallback_token_id
                side = 'buy'
            elif decision == 'market_make':
                token_id = yes_token or fallback_token_id
                pos = self.paper_engine.strategy_position(s.strategy_id, token_id)
                side = 'sell' if pos > 1e-9 else 'buy'
            elif decision == 'hold':
                continue
            else:
                token_id = yes_token or fallback_token_id
                side = 'buy'

            book = self._get_book_for_token(
                token_id,
                default_book=fallback_book if token_id == fallback_token_id else None,
            )
            if not isinstance(book, dict):
                self.strategy_store.append_log(
                    {
                        'kind': 'paper_bot_skip',
                        'strategy_id': s.strategy_id,
                        'strategy_type': s.strategy_type,
                        'market_id': market_id,
                        'token_id': token_id,
                        'tick': self._tick,
                        'reason': f'book_missing | {trigger_reason}',
                    }
                )
                continue
            bid, ask = best_bid_ask(book)
            if bid is None or ask is None:
                self.strategy_store.append_log(
                    {
                        'kind': 'paper_bot_skip',
                        'strategy_id': s.strategy_id,
                        'strategy_type': s.strategy_type,
                        'market_id': market_id,
                        'token_id': token_id,
                        'tick': self._tick,
                        'reason': f'no_bid_ask | {trigger_reason}',
                    }
                )
                continue
            mid = (bid + ask) / 2.0
            snap = self.paper_engine.account_snapshot(s.strategy_id)
            risk_limit = max(0.1, float(s.params.get('risk_loss_limit_pct', 3.0)))
            if float(snap.get('total_pnl_pct', 0.0)) <= -risk_limit:
                st['halted'] = True
                self.strategy_store.append_log(
                    {
                        'kind': 'paper_risk_halt',
                        'strategy_id': s.strategy_id,
                        'market_id': market_id,
                        'token_id': token_id,
                        'total_pnl_pct': snap.get('total_pnl_pct', 0.0),
                        'threshold_pct': -risk_limit,
                    }
                )
                return False

            qty, qty_reason = self._effective_order_qty(s, token_id=token_id, price=mid)
            if side == 'sell':
                qty = min(qty, max(0.0, self.paper_engine.strategy_position(s.strategy_id, token_id)))
            if qty <= 1e-12:
                self.strategy_store.append_log(
                    {
                        'kind': 'paper_bot_skip',
                        'strategy_id': s.strategy_id,
                        'strategy_type': s.strategy_type,
                        'market_id': market_id,
                        'token_id': token_id,
                        'tick': self._tick,
                        'reason': f'effective_qty<=0 | {trigger_reason}',
                    }
                )
                continue

            px = ask if side == 'buy' else bid
            decision_reason = f"{trigger_reason} | {qty_reason}"
            try:
                resp = self.paper_engine.place_limit_order(
                    strategy_id=s.strategy_id,
                    token_id=token_id,
                    side=side,
                    price=px,
                    size=qty,
                    order_type='bot',
                    source='paper_bot_workshop',
                    book=book,
                )
                self.strategy_store.append_log(
                    {
                        'kind': 'paper_bot_order',
                        'strategy_id': s.strategy_id,
                        'strategy_type': s.strategy_type,
                        'market_id': market_id,
                        'market_name': market_name,
                        'token_id': token_id,
                        'signal': side,
                        'price': px,
                        'size': qty,
                        'fills': len(resp.get('fills', [])),
                        'tick': self._tick,
                        'reason': decision_reason,
                        'source': 'stream' if used_stream else 'poll',
                    }
                )
                traded = True
            except Exception as exc:
                self.strategy_store.append_log(
                    {
                        'kind': 'paper_bot_order_error',
                        'strategy_id': s.strategy_id,
                        'strategy_type': s.strategy_type,
                        'market_id': market_id,
                        'market_name': market_name,
                        'token_id': token_id,
                        'signal': side,
                        'price': px,
                        'size': qty,
                        'tick': self._tick,
                        'reason': decision_reason,
                        'error': str(exc),
                    }
                )
        return traded

    def _one_tick(self) -> None:
        if not self._token_id:
            return
        token_id = self._token_id
        book: dict[str, Any] | None = None
        used_stream = False

        if self._prefer_stream:
            with self._lock:
                rec = self._stream_books.get(token_id, {})
                if isinstance(rec, dict):
                    ts = float(rec.get('mono_ts', 0.0))
                    if ts > 0 and (time.monotonic() - ts) <= max(20.0, self._interval_sec * 3.0):
                        maybe_book = rec.get('book')
                        if isinstance(maybe_book, dict):
                            book = maybe_book
                            used_stream = True

        if book is None:
            c = self.client_factory()
            polled = c.get_orderbook(token_id)
            if not isinstance(polled, dict):
                return
            book = polled
            self.paper_engine.on_book(token_id=token_id, book=book, source='paper_bot_tick')

        best_bid, best_ask = best_bid_ask(book)
        if best_bid is None or best_ask is None:
            self.strategy_store.append_log({'kind': 'paper_bot_skip', 'token_id': token_id, 'reason': 'no_bid_ask'})
            return
        mid = (best_bid + best_ask) / 2.0

        strategies = [x for x in self.strategy_store.load_strategies() if x.enabled]
        for s in strategies:
            st = self._state.setdefault(
                s.strategy_id,
                {'mids': [], 'entry_tick': -1, 'halted': False, 'last_eval_tick': 0},
            )
            if st.get('halted'):
                continue
            eval_interval_ticks = self._strategy_interval_ticks(s)
            last_eval_tick = int(st.get('last_eval_tick', 0))
            if (self._tick - last_eval_tick) < eval_interval_ticks:
                continue
            st['last_eval_tick'] = self._tick

            if self._is_workshop_strategy(s):
                self._execute_workshop_strategy(
                    s,
                    st,
                    fallback_token_id=token_id,
                    fallback_mid=mid,
                    fallback_book=book,
                    used_stream=used_stream,
                )
                continue

            signal, signal_reason = self._signal_for_strategy(s, st, mid=mid, token_id=token_id)
            if signal is None:
                continue

            snap = self.paper_engine.account_snapshot(s.strategy_id)
            risk_limit = max(0.1, float(s.params.get('risk_loss_limit_pct', 3.0)))
            if float(snap.get('total_pnl_pct', 0.0)) <= -risk_limit:
                st['halted'] = True
                self.strategy_store.append_log(
                    {
                        'kind': 'paper_risk_halt',
                        'strategy_id': s.strategy_id,
                        'token_id': token_id,
                        'total_pnl_pct': snap.get('total_pnl_pct', 0.0),
                        'threshold_pct': -risk_limit,
                    }
                )
                continue

            qty, qty_reason = self._effective_order_qty(s, token_id=token_id, price=mid)
            if signal == 'sell':
                qty = min(qty, max(0.0, self.paper_engine.strategy_position(s.strategy_id, token_id)))
            decision_notes = [x for x in [signal_reason, qty_reason] if x]

            max_total_notional = max(0.0, _safe_float(s.params.get('max_total_notional', 0.0), 0.0))
            if signal == 'buy' and max_total_notional > 0:
                exposure = 0.0
                for p in snap.get('positions', []):
                    if not isinstance(p, dict):
                        continue
                    exposure += abs(_safe_float(p.get('qty', 0.0)) * _safe_float(p.get('mark_price', 0.0)))
                remaining = max(0.0, max_total_notional - exposure)
                if remaining <= 1e-12:
                    self.strategy_store.append_log(
                        {
                            'kind': 'paper_bot_skip',
                            'strategy_id': s.strategy_id,
                            'strategy_type': s.strategy_type,
                            'token_id': token_id,
                            'tick': self._tick,
                            'reason': f'exposure_cap_reached exposure={exposure:.4f} max_total={max_total_notional:.4f}',
                        }
                    )
                    continue
                est_notional = qty * mid
                if est_notional > remaining and mid > 0:
                    prev_qty = qty
                    qty = remaining / mid
                    decision_notes.append(
                        f"exposure_cap_adjust {prev_qty:.6f}->{qty:.6f} (remaining={remaining:.4f})"
                    )
            if qty <= 1e-12:
                self.strategy_store.append_log(
                    {
                        'kind': 'paper_bot_skip',
                        'strategy_id': s.strategy_id,
                        'strategy_type': s.strategy_type,
                        'token_id': token_id,
                        'tick': self._tick,
                        'reason': 'effective_qty<=0',
                    }
                )
                continue

            px = best_ask if signal == 'buy' else best_bid
            try:
                resp = self.paper_engine.place_limit_order(
                    strategy_id=s.strategy_id,
                    token_id=token_id,
                    side=signal,
                    price=px,
                    size=qty,
                    order_type='bot',
                    source='paper_bot',
                    book=book,
                )
                if signal == 'buy' and resp.get('fills'):
                    st['entry_tick'] = self._tick
                if signal == 'sell' and self.paper_engine.strategy_position(s.strategy_id, token_id) <= 1e-9:
                    st['entry_tick'] = -1
                self.strategy_store.append_log(
                    {
                        'kind': 'paper_bot_order',
                        'strategy_id': s.strategy_id,
                        'strategy_type': s.strategy_type,
                        'token_id': token_id,
                        'signal': signal,
                        'price': px,
                        'size': qty,
                        'fills': len(resp.get('fills', [])),
                        'tick': self._tick,
                        'reason': ' | '.join(decision_notes),
                        'source': 'stream' if used_stream else 'poll',
                    }
                )
            except Exception as exc:
                self.strategy_store.append_log(
                    {
                        'kind': 'paper_bot_order_error',
                        'strategy_id': s.strategy_id,
                        'strategy_type': s.strategy_type,
                        'token_id': token_id,
                        'signal': signal,
                        'price': px,
                        'size': qty,
                        'tick': self._tick,
                        'reason': ' | '.join(decision_notes),
                        'error': str(exc),
                    }
                )
