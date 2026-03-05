from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json


Side = str


@dataclass
class Order:
    order_id: str
    token_id: str
    side: Side
    quantity: float
    limit_price: float
    remaining: float
    status: str
    created_tick: int
    placed_at_utc: str


@dataclass
class Fill:
    order_id: str
    token_id: str
    side: Side
    quantity: float
    price: float
    notional: float
    fee: float
    tick: int
    snapshot_file: str
    filled_at_utc: str


@dataclass
class SimulationConfig:
    initial_cash: float = 1000.0
    fee_bps: float = 2.0
    max_position_per_token: float = 200.0
    max_order_notional: float = 250.0
    order_ttl_ticks: int = 2
    strategy: str = 'periodic'
    risk_loss_limit_pct: float = 3.0
    mean_rev_window: int = 8
    mean_rev_threshold: float = 0.015


@dataclass
class Portfolio:
    cash: float
    positions: dict[str, float] = field(default_factory=dict)

    def position(self, token_id: str) -> float:
        return self.positions.get(token_id, 0.0)

    def apply_fill(self, fill: Fill) -> None:
        if fill.side == 'buy':
            self.cash -= fill.notional + fill.fee
            self.positions[fill.token_id] = self.position(fill.token_id) + fill.quantity
        else:
            self.cash += fill.notional - fill.fee
            self.positions[fill.token_id] = self.position(fill.token_id) - fill.quantity


@dataclass
class TokenStat:
    token_id: str
    buy_qty: float = 0.0
    buy_notional: float = 0.0
    sell_qty: float = 0.0
    sell_notional: float = 0.0
    fees: float = 0.0
    realized_pnl: float = 0.0
    net_position: float = 0.0
    avg_cost: float = 0.0


@dataclass
class SimulationResult:
    started_at_utc: str
    finished_at_utc: str
    snapshot_count: int
    token_universe: list[str]
    initial_cash: float
    final_cash: float
    final_equity: float
    realized_pnl: float
    max_drawdown_pct: float
    total_fees: float
    turnover: float
    buy_notional: float
    sell_notional: float
    trade_count: int
    win_rate: float
    winning_sells: int
    losing_sells: int
    risk_halted: bool
    risk_events: list[str]
    strategy: str
    open_positions: dict[str, float]
    open_orders: list[dict[str, Any]]
    token_stats: dict[str, dict[str, Any]]
    fills_count: int
    fills: list[dict[str, Any]]
    equity_curve: list[dict[str, Any]]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_div(a: float, b: float) -> float:
    if abs(b) <= 1e-12:
        return 0.0
    return a / b


def _parse_price_levels(book: dict[str, Any], side: Side) -> list[tuple[float, float]]:
    key = 'asks' if side == 'buy' else 'bids'
    raw = book.get(key, [])
    levels: list[tuple[float, float]] = []

    if not isinstance(raw, list):
        return levels

    for level in raw:
        if not isinstance(level, dict):
            continue
        try:
            price = float(level.get('price', 0.0))
            size = float(level.get('size', 0.0))
        except (TypeError, ValueError):
            continue
        if price <= 0 or size <= 0:
            continue
        levels.append((price, size))

    if side == 'buy':
        levels.sort(key=lambda x: x[0])
    else:
        levels.sort(key=lambda x: x[0], reverse=True)

    return levels


def best_bid_ask(book: dict[str, Any]) -> tuple[float | None, float | None]:
    bids = _parse_price_levels(book, side='sell')
    asks = _parse_price_levels(book, side='buy')
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    return best_bid, best_ask


class PaperSimulator:
    def __init__(self, config: SimulationConfig) -> None:
        self.config = config
        self.portfolio = Portfolio(cash=config.initial_cash)
        self.order_seq = 0
        self.open_orders: list[Order] = []
        self.fills: list[Fill] = []
        self.equity_curve: list[dict[str, Any]] = []

        self.token_stats: dict[str, TokenStat] = {}
        self.position_cost: dict[str, float] = {}
        self.total_fees = 0.0
        self.buy_notional = 0.0
        self.sell_notional = 0.0
        self.winning_sells = 0
        self.losing_sells = 0

        self.risk_halted = False
        self.risk_events: list[str] = []

    def _next_order_id(self) -> str:
        self.order_seq += 1
        return f'ord-{self.order_seq:06d}'

    def _stat(self, token_id: str) -> TokenStat:
        if token_id not in self.token_stats:
            self.token_stats[token_id] = TokenStat(token_id=token_id)
        return self.token_stats[token_id]

    def _record_fill_stats(self, fill: Fill) -> None:
        stat = self._stat(fill.token_id)
        stat.fees += fill.fee
        self.total_fees += fill.fee

        current_pos = self.portfolio.position(fill.token_id)
        avg_cost = self.position_cost.get(fill.token_id, 0.0)

        if fill.side == 'buy':
            stat.buy_qty += fill.quantity
            stat.buy_notional += fill.notional
            self.buy_notional += fill.notional

            new_pos = current_pos + fill.quantity
            if new_pos > 1e-12:
                new_avg = _safe_div(avg_cost * current_pos + fill.notional, new_pos)
                self.position_cost[fill.token_id] = new_avg
            else:
                self.position_cost[fill.token_id] = 0.0
        else:
            stat.sell_qty += fill.quantity
            stat.sell_notional += fill.notional
            self.sell_notional += fill.notional

            closed_qty = min(fill.quantity, max(current_pos, 0.0))
            unit_pnl = fill.price - avg_cost
            realized = unit_pnl * closed_qty
            stat.realized_pnl += realized

            if unit_pnl > 1e-12:
                self.winning_sells += 1
            elif unit_pnl < -1e-12:
                self.losing_sells += 1

            new_pos = current_pos - fill.quantity
            if abs(new_pos) <= 1e-12:
                self.position_cost[fill.token_id] = 0.0
            elif new_pos < 0:
                self.position_cost[fill.token_id] = fill.price

        stat.net_position = self.portfolio.position(fill.token_id)
        stat.avg_cost = self.position_cost.get(fill.token_id, 0.0)

    def place_limit_order(
        self,
        token_id: str,
        side: Side,
        quantity: float,
        limit_price: float,
        tick: int,
        now_utc: str,
    ) -> Order | None:
        if quantity <= 0 or limit_price <= 0:
            return None

        order_notional = quantity * limit_price
        if order_notional > self.config.max_order_notional:
            return None

        curr_pos = self.portfolio.position(token_id)
        projected = curr_pos + quantity if side == 'buy' else curr_pos - quantity
        if abs(projected) > self.config.max_position_per_token:
            return None

        if side == 'buy':
            max_affordable = self.portfolio.cash / (limit_price * (1.0 + self.config.fee_bps / 10000.0))
            if max_affordable <= 0:
                return None
            quantity = min(quantity, max_affordable)
            if quantity <= 0:
                return None

        order = Order(
            order_id=self._next_order_id(),
            token_id=token_id,
            side=side,
            quantity=quantity,
            limit_price=limit_price,
            remaining=quantity,
            status='open',
            created_tick=tick,
            placed_at_utc=now_utc,
        )
        self.open_orders.append(order)
        return order

    def _match_order(
        self,
        order: Order,
        book: dict[str, Any],
        tick: int,
        snapshot_file: str,
        now_utc: str,
    ) -> list[Fill]:
        levels = _parse_price_levels(book, side=order.side)
        if not levels:
            return []

        matched: list[Fill] = []
        rem = order.remaining

        for price, size in levels:
            crosses = price <= order.limit_price if order.side == 'buy' else price >= order.limit_price
            if not crosses:
                break
            if rem <= 0:
                break

            qty = min(rem, size)
            if qty <= 0:
                continue

            notional = qty * price
            fee = notional * self.config.fee_bps / 10000.0
            fill = Fill(
                order_id=order.order_id,
                token_id=order.token_id,
                side=order.side,
                quantity=qty,
                price=price,
                notional=notional,
                fee=fee,
                tick=tick,
                snapshot_file=snapshot_file,
                filled_at_utc=now_utc,
            )
            self.portfolio.apply_fill(fill)
            self._record_fill_stats(fill)
            matched.append(fill)
            rem -= qty

        order.remaining = rem
        if order.remaining <= 1e-12:
            order.remaining = 0.0
            order.status = 'filled'
        elif matched:
            order.status = 'partial'

        return matched

    def _expire_orders(self, tick: int) -> None:
        for order in self.open_orders:
            if order.status in {'filled', 'cancelled'}:
                continue
            if tick - order.created_tick >= self.config.order_ttl_ticks:
                order.status = 'cancelled'

    def _mark_equity(self, books: dict[str, Any], tick: int, now_utc: str) -> float:
        mtm = self.portfolio.cash
        for token_id, qty in self.portfolio.positions.items():
            if abs(qty) <= 1e-12:
                continue
            book = books.get(token_id)
            if not isinstance(book, dict):
                continue
            best_bid, best_ask = best_bid_ask(book)
            if best_bid is None and best_ask is None:
                continue
            mark = (best_bid + best_ask) / 2.0 if best_bid is not None and best_ask is not None else (best_bid or best_ask)
            mtm += qty * mark

        self.equity_curve.append({'tick': tick, 'time_utc': now_utc, 'equity': mtm, 'cash': self.portfolio.cash})
        return mtm

    def _max_drawdown_pct(self) -> float:
        if not self.equity_curve:
            return 0.0

        peak = self.equity_curve[0]['equity']
        max_dd = 0.0
        for point in self.equity_curve:
            equity = point['equity']
            if equity > peak:
                peak = equity
            if peak > 1e-12:
                dd = (peak - equity) / peak
                if dd > max_dd:
                    max_dd = dd
        return max_dd * 100.0

    def _periodic_signal(self, tick: int, token_id: str, pos: float, last_entry: int | None, hold_ticks: int) -> str | None:
        if pos <= 1e-12:
            should_enter = last_entry is None or tick % (hold_ticks + 1) == 0
            return 'buy' if should_enter else None
        if last_entry is not None and tick - last_entry >= hold_ticks:
            return 'sell'
        return None

    def _mean_reversion_signal(
        self,
        tick: int,
        token_id: str,
        mid: float,
        pos: float,
        mid_history: dict[str, list[float]],
    ) -> str | None:
        history = mid_history.setdefault(token_id, [])
        history.append(mid)
        if len(history) > max(2, self.config.mean_rev_window):
            history.pop(0)

        if len(history) < max(3, self.config.mean_rev_window // 2):
            return None

        avg_mid = sum(history) / len(history)
        low = avg_mid * (1.0 - self.config.mean_rev_threshold)
        high = avg_mid * (1.0 + self.config.mean_rev_threshold)

        if pos <= 1e-12 and mid < low:
            return 'buy'
        if pos > 1e-12 and mid > high:
            return 'sell'
        return None

    def _strategy_signal(
        self,
        tick: int,
        token_id: str,
        pos: float,
        last_entry: int | None,
        hold_ticks: int,
        mid: float,
        mid_history: dict[str, list[float]],
    ) -> str | None:
        if self.config.strategy == 'mean_reversion':
            return self._mean_reversion_signal(tick, token_id, mid, pos, mid_history)
        return self._periodic_signal(tick, token_id, pos, last_entry, hold_ticks)

    def _maybe_trigger_risk_halt(self, equity: float, tick: int, now_utc: str) -> None:
        if self.risk_halted:
            return
        loss_pct = (self.config.initial_cash - equity) / self.config.initial_cash * 100.0
        if loss_pct >= self.config.risk_loss_limit_pct:
            self.risk_halted = True
            self.risk_events.append(
                f"[{now_utc}] tick={tick} 触发风险熔断: loss={loss_pct:.3f}% 阈值={self.config.risk_loss_limit_pct:.3f}%"
            )

    def run(
        self,
        snapshots: list[tuple[str, dict[str, Any]]],
        token_universe: list[str],
        order_qty: float,
        hold_ticks: int,
    ) -> SimulationResult:
        started = _now_iso()
        last_buy_tick: dict[str, int | None] = {t: None for t in token_universe}
        mid_history: dict[str, list[float]] = {}

        for tick, (snapshot_file, snapshot) in enumerate(snapshots):
            now_utc = snapshot.get('fetched_at_utc') or _now_iso()
            books = snapshot.get('books', {})
            if not isinstance(books, dict):
                books = {}

            for order in list(self.open_orders):
                if order.status in {'filled', 'cancelled'}:
                    continue
                book = books.get(order.token_id)
                if not isinstance(book, dict):
                    continue
                new_fills = self._match_order(order, book, tick=tick, snapshot_file=snapshot_file, now_utc=now_utc)
                self.fills.extend(new_fills)

            for token_id in token_universe:
                book = books.get(token_id)
                if not isinstance(book, dict):
                    continue
                best_bid, best_ask = best_bid_ask(book)
                if best_bid is None or best_ask is None:
                    continue

                pos = self.portfolio.position(token_id)
                last_entry = last_buy_tick.get(token_id)
                mid = (best_bid + best_ask) / 2.0

                signal = self._strategy_signal(
                    tick=tick,
                    token_id=token_id,
                    pos=pos,
                    last_entry=last_entry,
                    hold_ticks=hold_ticks,
                    mid=mid,
                    mid_history=mid_history,
                )

                if signal == 'buy' and not self.risk_halted:
                    order = self.place_limit_order(
                        token_id=token_id,
                        side='buy',
                        quantity=order_qty,
                        limit_price=best_ask,
                        tick=tick,
                        now_utc=now_utc,
                    )
                    if order is not None:
                        new_fills = self._match_order(order, book, tick=tick, snapshot_file=snapshot_file, now_utc=now_utc)
                        self.fills.extend(new_fills)
                        if order.status in {'filled', 'partial'}:
                            last_buy_tick[token_id] = tick

                elif signal == 'sell' and pos > 1e-12:
                    order = self.place_limit_order(
                        token_id=token_id,
                        side='sell',
                        quantity=max(0.0, pos),
                        limit_price=best_bid,
                        tick=tick,
                        now_utc=now_utc,
                    )
                    if order is not None:
                        new_fills = self._match_order(order, book, tick=tick, snapshot_file=snapshot_file, now_utc=now_utc)
                        self.fills.extend(new_fills)

            self._expire_orders(tick)
            equity = self._mark_equity(books, tick=tick, now_utc=now_utc)
            self._maybe_trigger_risk_halt(equity, tick=tick, now_utc=now_utc)

        finished = _now_iso()
        final_equity = self.equity_curve[-1]['equity'] if self.equity_curve else self.portfolio.cash

        open_orders = [asdict(o) for o in self.open_orders if o.status not in {'filled', 'cancelled'}]
        open_positions = {k: v for k, v in self.portfolio.positions.items() if abs(v) > 1e-12}
        closed_sell_count = self.winning_sells + self.losing_sells

        return SimulationResult(
            started_at_utc=started,
            finished_at_utc=finished,
            snapshot_count=len(snapshots),
            token_universe=token_universe,
            initial_cash=self.config.initial_cash,
            final_cash=self.portfolio.cash,
            final_equity=final_equity,
            realized_pnl=final_equity - self.config.initial_cash,
            max_drawdown_pct=self._max_drawdown_pct(),
            total_fees=self.total_fees,
            turnover=self.buy_notional + self.sell_notional,
            buy_notional=self.buy_notional,
            sell_notional=self.sell_notional,
            trade_count=len(self.fills),
            win_rate=_safe_div(self.winning_sells, closed_sell_count),
            winning_sells=self.winning_sells,
            losing_sells=self.losing_sells,
            risk_halted=self.risk_halted,
            risk_events=self.risk_events,
            strategy=self.config.strategy,
            open_positions=open_positions,
            open_orders=open_orders,
            token_stats={k: asdict(v) for k, v in self.token_stats.items()},
            fills_count=len(self.fills),
            fills=[asdict(f) for f in self.fills],
            equity_curve=self.equity_curve,
        )


def load_snapshots(snapshot_paths: list[Path]) -> list[tuple[str, dict[str, Any]]]:
    snapshots: list[tuple[str, dict[str, Any]]] = []
    for path in snapshot_paths:
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        snapshots.append((str(path), payload))
    return snapshots
