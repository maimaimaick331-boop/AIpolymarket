from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Any

from libs.connectors.polymarket_live import PolymarketLiveClient
from libs.services.live_strategy_service import LiveStrategyStore, StrategyConfig


@dataclass
class BotStatus:
    running: bool
    token_id: str
    interval_sec: int
    tick: int


class LiveBotManager:
    def __init__(
        self,
        client_factory,
        strategy_store: LiveStrategyStore,
        max_order_usdc: float,
    ) -> None:
        self.client_factory = client_factory
        self.strategy_store = strategy_store
        self.max_order_usdc = max_order_usdc
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_flag = threading.Event()
        self._token_id = ''
        self._interval_sec = 20
        self._tick = 0
        self._state: dict[str, dict[str, Any]] = {}

    def start(self, token_id: str, interval_sec: int = 20) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._token_id = token_id
            self._interval_sec = max(2, interval_sec)
            self._tick = 0
            self._stop_flag.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            self.strategy_store.append_log({'kind': 'bot_start', 'token_id': token_id, 'interval_sec': self._interval_sec})

    def stop(self) -> None:
        with self._lock:
            self._stop_flag.set()
            t = self._thread
        if t and t.is_alive():
            t.join(timeout=5)
        self.strategy_store.append_log({'kind': 'bot_stop'})

    def status(self) -> BotStatus:
        alive = bool(self._thread and self._thread.is_alive() and not self._stop_flag.is_set())
        return BotStatus(running=alive, token_id=self._token_id, interval_sec=self._interval_sec, tick=self._tick)

    def _run(self) -> None:
        while not self._stop_flag.is_set():
            self._tick += 1
            try:
                self._one_tick()
            except Exception as exc:
                self.strategy_store.append_log({'kind': 'bot_error', 'error': str(exc), 'tick': self._tick})
            time.sleep(self._interval_sec)

    def _one_tick(self) -> None:
        c: PolymarketLiveClient = self.client_factory()
        book = c.get_order_book(self._token_id)
        bids = book.get('bids', []) if isinstance(book, dict) else getattr(book, 'bids', [])
        asks = book.get('asks', []) if isinstance(book, dict) else getattr(book, 'asks', [])

        best_bid = float(bids[0]['price']) if bids else None
        best_ask = float(asks[0]['price']) if asks else None
        if best_bid is None or best_ask is None:
            self.strategy_store.append_log({'kind': 'bot_skip', 'reason': 'no_book', 'token_id': self._token_id})
            return

        mid = (best_bid + best_ask) / 2.0
        strategies = [s for s in self.strategy_store.load_strategies() if s.enabled]

        for s in strategies:
            st = self._state.setdefault(s.strategy_id, {'pos': 0.0, 'entry_tick': -1, 'mids': []})
            signal = self._signal_for_strategy(s, st, mid)
            if signal is None:
                continue

            qty = float(s.params.get('order_qty', 2.0))
            qty = max(0.1, qty)
            notional = qty * (best_ask if signal == 'buy' else best_bid)
            if notional > self.max_order_usdc:
                self.strategy_store.append_log(
                    {
                        'kind': 'bot_skip',
                        'strategy_id': s.strategy_id,
                        'reason': 'max_order_usdc',
                        'notional': notional,
                        'limit': self.max_order_usdc,
                    }
                )
                continue

            side = 'buy' if signal == 'buy' else 'sell'
            price = best_ask if side == 'buy' else best_bid
            size = qty if side == 'buy' else min(qty, max(0.0, float(st.get('pos', 0.0))))
            if size <= 0:
                continue

            try:
                resp = c.place_limit_order(token_id=self._token_id, side=side, price=price, size=size, order_type='GTC')
                if side == 'buy':
                    st['pos'] = float(st.get('pos', 0.0)) + size
                    st['entry_tick'] = self._tick
                else:
                    st['pos'] = max(0.0, float(st.get('pos', 0.0)) - size)

                self.strategy_store.append_log(
                    {
                        'kind': 'bot_order',
                        'strategy_id': s.strategy_id,
                        'strategy_type': s.strategy_type,
                        'token_id': self._token_id,
                        'signal': side,
                        'price': price,
                        'size': size,
                        'tick': self._tick,
                        'response': resp,
                    }
                )
            except Exception as exc:
                self.strategy_store.append_log(
                    {
                        'kind': 'bot_order_error',
                        'strategy_id': s.strategy_id,
                        'strategy_type': s.strategy_type,
                        'token_id': self._token_id,
                        'signal': side,
                        'price': price,
                        'size': size,
                        'tick': self._tick,
                        'error': str(exc),
                    }
                )

    def _signal_for_strategy(self, s: StrategyConfig, st: dict[str, Any], mid: float) -> str | None:
        st['mids'].append(mid)
        if len(st['mids']) > 30:
            st['mids'].pop(0)

        pos = float(st.get('pos', 0.0))

        if s.strategy_type == 'mean_reversion':
            window = int(s.params.get('mean_rev_window', 8))
            th = float(s.params.get('mean_rev_threshold', 0.015))
            hist = st['mids'][-max(3, window) :]
            if len(hist) < max(3, window // 2):
                return None
            avg = sum(hist) / len(hist)
            if pos <= 1e-9 and mid < avg * (1.0 - th):
                return 'buy'
            if pos > 1e-9 and mid > avg * (1.0 + th):
                return 'sell'
            return None

        hold_ticks = int(s.params.get('hold_ticks', 2))
        if pos <= 1e-9:
            return 'buy'
        if self._tick - int(st.get('entry_tick', 0)) >= max(1, hold_ticks):
            return 'sell'
        return None
