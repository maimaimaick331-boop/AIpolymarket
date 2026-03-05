from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable
import math

from libs.quant.db import QuantDB


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _normalize_book(book: dict[str, Any]) -> dict[str, Any]:
    def _rows(v: Any, reverse: bool = False) -> list[dict[str, float]]:
        out: list[dict[str, float]] = []
        if not isinstance(v, list):
            return out
        for row in v:
            if not isinstance(row, dict):
                continue
            p = _safe_float(row.get('price'))
            q = _safe_float(row.get('size'))
            if p <= 0 or q <= 0:
                continue
            out.append({'price': p, 'size': q})
        out.sort(key=lambda x: x['price'], reverse=reverse)
        return out

    return {'bids': _rows(book.get('bids', []), reverse=True), 'asks': _rows(book.get('asks', []), reverse=False)}


def _align_price_tick(price: float, tick_size: float, side: str) -> float:
    px = max(0.0001, float(price))
    tick = max(0.0000001, float(tick_size))
    units = px / tick
    if str(side or '').strip().lower() == 'buy':
        out = math.floor(units + 1e-9) * tick
    else:
        out = math.ceil(units - 1e-9) * tick
    return max(0.0001, round(out, 8))


class ExecutionEngine:
    def __init__(
        self,
        *,
        db: QuantDB,
        paper_engine: Any,
        market_data_engine: Any,
        public_client_factory: Callable[[], Any],
        live_client_factory: Callable[[], Any],
    ) -> None:
        self.db = db
        self.paper_engine = paper_engine
        self.market_data_engine = market_data_engine
        self.public_client_factory = public_client_factory
        self.live_client_factory = live_client_factory

    def _book_for_token(self, token_id: str) -> dict[str, Any] | None:
        book = self.market_data_engine.get_book(token_id)
        if isinstance(book, dict) and book.get('bids') and book.get('asks'):
            return _normalize_book(book)
        try:
            c = self.public_client_factory()
            raw = c.get_orderbook(token_id)
            if isinstance(raw, dict):
                return _normalize_book(raw)
        except Exception:
            return None
        return None

    def execute(
        self,
        *,
        signal: dict[str, Any],
        signal_id: int,
        size_usdc: float,
        mode: str = 'paper',
    ) -> dict[str, Any]:
        mode_norm = str(mode or 'paper').strip().lower()
        if mode_norm == 'live':
            return self._execute_live(signal=signal, signal_id=signal_id, size_usdc=size_usdc)
        return self._execute_paper(signal=signal, signal_id=signal_id, size_usdc=size_usdc)

    def _execute_paper(self, *, signal: dict[str, Any], signal_id: int, size_usdc: float) -> dict[str, Any]:
        strategy_id = str(signal.get('strategy_id', '')).strip() or 'auto'
        token_id = str(signal.get('token_id', '')).strip()
        side = str(signal.get('side', 'BUY')).strip().lower()
        order_kind = str(signal.get('order_kind', 'limit')).strip().lower()
        if side not in {'buy', 'sell'}:
            raise ValueError(f'非法 side: {side}')
        if not token_id:
            raise ValueError('token_id 不能为空')

        pre = self.paper_engine.account_snapshot(strategy_id)
        pre_pnl = _safe_float(pre.get('total_pnl', 0.0))
        book = self._book_for_token(token_id)
        if not isinstance(book, dict):
            raise ValueError('缺少可用 orderbook，无法执行信号')

        if order_kind == 'market':
            order_out = self.paper_engine.place_market_order(
                strategy_id=strategy_id,
                token_id=token_id,
                side=side,
                amount=max(1e-6, float(size_usdc)),
                order_type='FOK',
                source='quant_auto',
                book=book,
            )
            req_price = None
            req_size = None
            req_amount = float(size_usdc)
            order_type = 'FOK'
        else:
            req_price = _safe_float(signal.get('price', 0.0))
            if req_price <= 0:
                bids = book.get('bids', [])
                asks = book.get('asks', [])
                if side == 'buy' and isinstance(asks, list) and asks:
                    req_price = _safe_float(asks[0].get('price', 0.0))
                elif side == 'sell' and isinstance(bids, list) and bids:
                    req_price = _safe_float(bids[0].get('price', 0.0))
            if req_price <= 0:
                raise ValueError('limit 信号缺少有效 price')
            req_size = max(1e-6, float(size_usdc) / req_price)
            rule = self.paper_engine.token_rule(token_id)
            tick_size = _safe_float((rule or {}).get('tick_size', 0.0))
            if tick_size > 0:
                req_price = _align_price_tick(req_price, tick_size, side)
            min_size = _safe_float((rule or {}).get('min_size', 0.0))
            if min_size > 0 and req_size < min_size:
                req_size = min_size
            order_out = self.paper_engine.place_limit_order(
                strategy_id=strategy_id,
                token_id=token_id,
                side=side,
                price=req_price,
                size=req_size,
                order_type='GTC',
                source='quant_auto',
                book=book,
            )
            req_amount = None
            order_type = 'GTC'

        order = order_out.get('order', {}) if isinstance(order_out, dict) else {}
        fills = order_out.get('fills', []) if isinstance(order_out, dict) else []
        if not isinstance(order, dict):
            order = {}
        if not isinstance(fills, list):
            fills = []

        db_order_id = self.db.insert_order(
            {
                'time_utc': _now_utc(),
                'mode': 'paper',
                'strategy_id': strategy_id,
                'signal_id': signal_id,
                'token_id': token_id,
                'side': side.upper(),
                'order_kind': order_kind,
                'order_type': order_type,
                'order_id': str(order.get('order_id', '')),
                'price': req_price,
                'size': req_size,
                'amount': req_amount,
                'status': str(order.get('status', '')),
                'raw': order_out if isinstance(order_out, dict) else {'value': str(order_out)},
            }
        )

        post = self.paper_engine.account_snapshot(strategy_id)
        post_pnl = _safe_float(post.get('total_pnl', 0.0))
        pnl_delta = post_pnl - pre_pnl

        for fill in fills:
            if not isinstance(fill, dict):
                continue
            self.db.insert_fill(
                {
                    'time_utc': str(fill.get('time_utc', _now_utc())),
                    'mode': 'paper',
                    'strategy_id': strategy_id,
                    'signal_id': signal_id,
                    'order_id': str(fill.get('order_id', '')),
                    'fill_id': str(fill.get('fill_id', '')),
                    'token_id': token_id,
                    'side': str(fill.get('side', side)).upper(),
                    'price': _safe_float(fill.get('price', 0.0)),
                    'size': _safe_float(fill.get('quantity', fill.get('size', 0.0))),
                    'notional': _safe_float(fill.get('notional', 0.0)),
                    'fee': _safe_float(fill.get('fee', 0.0)),
                    'pnl_delta': pnl_delta / max(1, len(fills)),
                    'raw': fill,
                }
            )

        return {
            'ok': True,
            'mode': 'paper',
            'signal_id': signal_id,
            'db_order_id': db_order_id,
            'order': order,
            'fills_count': len(fills),
            'pnl_delta': pnl_delta,
            'strategy_id': strategy_id,
        }

    def _execute_live(self, *, signal: dict[str, Any], signal_id: int, size_usdc: float) -> dict[str, Any]:
        strategy_id = str(signal.get('strategy_id', '')).strip() or 'auto'
        token_id = str(signal.get('token_id', '')).strip()
        side = str(signal.get('side', 'BUY')).strip().lower()
        order_kind = str(signal.get('order_kind', 'limit')).strip().lower()
        if side not in {'buy', 'sell'}:
            raise ValueError(f'非法 side: {side}')
        if not token_id:
            raise ValueError('token_id 不能为空')
        c = self.live_client_factory()

        if order_kind == 'market':
            resp = c.place_market_order(
                token_id=token_id,
                side=side,
                amount=max(1e-6, float(size_usdc)),
                order_type='FOK',
            )
            req_price = None
            req_size = None
            req_amount = float(size_usdc)
            order_type = 'FOK'
        else:
            price = _safe_float(signal.get('price', 0.0))
            if price <= 0:
                raise ValueError('live limit 信号缺少 price')
            size = max(1e-6, float(size_usdc) / price)
            resp = c.place_limit_order(
                token_id=token_id,
                side=side,
                price=price,
                size=size,
                order_type='GTC',
            )
            req_price = price
            req_size = size
            req_amount = None
            order_type = 'GTC'

        self.db.insert_order(
            {
                'time_utc': _now_utc(),
                'mode': 'live',
                'strategy_id': strategy_id,
                'signal_id': signal_id,
                'token_id': token_id,
                'side': side.upper(),
                'order_kind': order_kind,
                'order_type': order_type,
                'order_id': str((resp or {}).get('orderID', (resp or {}).get('id', ''))),
                'price': req_price,
                'size': req_size,
                'amount': req_amount,
                'status': str((resp or {}).get('status', 'submitted')),
                'raw': resp if isinstance(resp, dict) else {'value': str(resp)},
            }
        )
        return {
            'ok': True,
            'mode': 'live',
            'signal_id': signal_id,
            'response': resp,
            'fills_count': 0,
            'pnl_delta': 0.0,
            'strategy_id': strategy_id,
        }
