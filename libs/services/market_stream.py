from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
import asyncio
import json
import threading
import time

import websockets


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


@dataclass
class MarketStreamStatus:
    running: bool
    connected: bool
    endpoint: str
    custom_feature_enabled: bool
    subscribed_assets: list[str]
    recv_total: int
    book_events: int
    price_change_events: int
    tick_size_events: int
    last_event_type: str
    last_event_utc: str
    last_error: str
    reconnect_count: int
    ping_sent: int
    pong_recv: int


class PolymarketMarketStream:
    def __init__(
        self,
        endpoint: str,
        *,
        custom_feature_enabled: bool = True,
        on_book: Callable[[str, dict[str, Any], str, dict[str, Any]], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.endpoint = endpoint.strip()
        self.custom_feature_enabled = bool(custom_feature_enabled)
        self.on_book = on_book
        self.on_event = on_event

        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._connected = False
        self._subscribed_assets: set[str] = set()
        self._sub_version = 0
        self._books: dict[str, dict[str, Any]] = {}

        self._recv_total = 0
        self._book_events = 0
        self._price_change_events = 0
        self._tick_size_events = 0
        self._last_event_type = ''
        self._last_event_utc = ''
        self._last_error = ''
        self._reconnect_count = 0
        self._ping_sent = 0
        self._pong_recv = 0

    def configure(self, custom_feature_enabled: bool | None = None) -> None:
        with self._lock:
            if custom_feature_enabled is not None:
                self.custom_feature_enabled = bool(custom_feature_enabled)
                self._sub_version += 1

    def start(self, assets_ids: list[str] | None = None, *, custom_feature_enabled: bool | None = None) -> None:
        with self._lock:
            if custom_feature_enabled is not None:
                self.custom_feature_enabled = bool(custom_feature_enabled)
            if assets_ids:
                for a in assets_ids:
                    aid = str(a or '').strip()
                    if aid:
                        self._subscribed_assets.add(aid)
                self._sub_version += 1
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run_thread, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            t = self._thread
        if t and t.is_alive():
            t.join(timeout=5)
        with self._lock:
            self._connected = False

    def set_assets(self, assets_ids: list[str]) -> None:
        rows = {str(a or '').strip() for a in assets_ids}
        rows = {x for x in rows if x}
        with self._lock:
            self._subscribed_assets = rows
            self._sub_version += 1

    def add_assets(self, assets_ids: list[str]) -> None:
        changed = False
        with self._lock:
            for a in assets_ids:
                aid = str(a or '').strip()
                if aid and aid not in self._subscribed_assets:
                    self._subscribed_assets.add(aid)
                    changed = True
            if changed:
                self._sub_version += 1

    def remove_assets(self, assets_ids: list[str]) -> None:
        changed = False
        with self._lock:
            for a in assets_ids:
                aid = str(a or '').strip()
                if aid and aid in self._subscribed_assets:
                    self._subscribed_assets.remove(aid)
                    changed = True
            if changed:
                self._sub_version += 1

    def status(self) -> MarketStreamStatus:
        with self._lock:
            running = bool(self._thread and self._thread.is_alive() and not self._stop.is_set())
            return MarketStreamStatus(
                running=running,
                connected=self._connected,
                endpoint=self.endpoint,
                custom_feature_enabled=self.custom_feature_enabled,
                subscribed_assets=sorted(self._subscribed_assets),
                recv_total=self._recv_total,
                book_events=self._book_events,
                price_change_events=self._price_change_events,
                tick_size_events=self._tick_size_events,
                last_event_type=self._last_event_type,
                last_event_utc=self._last_event_utc,
                last_error=self._last_error,
                reconnect_count=self._reconnect_count,
                ping_sent=self._ping_sent,
                pong_recv=self._pong_recv,
            )

    def _run_thread(self) -> None:
        asyncio.run(self._run())

    async def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                assets = sorted(self._subscribed_assets)
                sub_version = self._sub_version
                custom_feature_enabled = self.custom_feature_enabled

            if not assets:
                await asyncio.sleep(0.5)
                continue

            try:
                async with websockets.connect(
                    self.endpoint,
                    ping_interval=None,
                    ping_timeout=None,
                    close_timeout=3,
                    max_size=8 * 1024 * 1024,
                ) as ws:
                    with self._lock:
                        self._connected = True
                        self._last_error = ''

                    sub_msg = {
                        'assets_ids': assets,
                        'type': 'market',
                        'custom_feature_enabled': bool(custom_feature_enabled),
                    }
                    await ws.send(json.dumps(sub_msg))

                    ping_task = asyncio.create_task(self._ping_loop(ws))
                    try:
                        while not self._stop.is_set():
                            with self._lock:
                                if self._sub_version != sub_version:
                                    break
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                            except asyncio.TimeoutError:
                                continue

                            if isinstance(raw, bytes):
                                raw = raw.decode('utf-8', errors='ignore')
                            text = str(raw or '').strip()
                            if not text:
                                continue
                            if text.upper() == 'PONG':
                                with self._lock:
                                    self._pong_recv += 1
                                continue
                            if text.upper() == 'PING':
                                await ws.send('PONG')
                                continue

                            try:
                                payload = json.loads(text)
                            except Exception:
                                continue
                            if not isinstance(payload, dict):
                                continue
                            self._handle_payload(payload)
                    finally:
                        ping_task.cancel()
                        with self._lock:
                            self._connected = False
            except Exception as exc:
                with self._lock:
                    self._connected = False
                    self._last_error = str(exc)
                    self._reconnect_count += 1
                await asyncio.sleep(1.5)

    async def _ping_loop(self, ws) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(10)
            try:
                await ws.send('PING')
                with self._lock:
                    self._ping_sent += 1
            except Exception:
                return

    def _handle_payload(self, payload: dict[str, Any]) -> None:
        event_type = str(payload.get('event_type') or payload.get('type') or '').strip().lower()
        with self._lock:
            self._recv_total += 1
            self._last_event_type = event_type
            self._last_event_utc = _now()

        if self.on_event is not None:
            try:
                self.on_event(payload)
            except Exception:
                pass

        if event_type == 'book':
            asset_id = str(payload.get('asset_id', '')).strip()
            if not asset_id:
                return
            book = self._normalize_book(payload)
            self._set_book(asset_id, book)
            with self._lock:
                self._book_events += 1
            self._emit_book(asset_id=asset_id, book=book, source='ws_book', payload=payload)
            return

        if event_type == 'price_change':
            changes = payload.get('price_changes', payload.get('changes', []))
            if not isinstance(changes, list):
                return
            touched: set[str] = set()
            for ch in changes:
                if not isinstance(ch, dict):
                    continue
                asset_id = str(ch.get('asset_id', payload.get('asset_id', ''))).strip()
                if not asset_id:
                    continue
                side = str(ch.get('side', '')).upper()
                price = _safe_float(ch.get('price'))
                size = _safe_float(ch.get('size'))
                if price <= 0:
                    continue
                self._apply_price_change(asset_id=asset_id, side=side, price=price, size=size)
                touched.add(asset_id)
            for asset_id in touched:
                book = self._get_book_snapshot(asset_id)
                if book is None:
                    continue
                self._emit_book(asset_id=asset_id, book=book, source='ws_price_change', payload=payload)
            if touched:
                with self._lock:
                    self._price_change_events += 1
            return

        if event_type == 'tick_size_change':
            with self._lock:
                self._tick_size_events += 1
            return

    def _normalize_book(self, payload: dict[str, Any]) -> dict[str, Any]:
        def _rows(items: Any) -> list[dict[str, str]]:
            out: list[dict[str, str]] = []
            if not isinstance(items, list):
                return out
            for row in items:
                if not isinstance(row, dict):
                    continue
                price = _safe_float(row.get('price'))
                size = _safe_float(row.get('size'))
                if price <= 0 or size <= 0:
                    continue
                out.append({'price': f'{price:.12g}', 'size': f'{size:.12g}'})
            return out

        bids = payload.get('bids', payload.get('buys', []))
        asks = payload.get('asks', payload.get('sells', []))
        return {
            'bids': _rows(bids),
            'asks': _rows(asks),
            'timestamp': payload.get('timestamp', ''),
            'hash': payload.get('hash', ''),
            'event_type': payload.get('event_type', ''),
        }

    def _set_book(self, asset_id: str, book: dict[str, Any]) -> None:
        bids_map = {str(x['price']): _safe_float(x['size']) for x in book.get('bids', []) if isinstance(x, dict)}
        asks_map = {str(x['price']): _safe_float(x['size']) for x in book.get('asks', []) if isinstance(x, dict)}
        with self._lock:
            self._books[asset_id] = {
                'bids': bids_map,
                'asks': asks_map,
                'updated_at_utc': _now(),
            }

    def _apply_price_change(self, *, asset_id: str, side: str, price: float, size: float) -> None:
        side = side.upper().strip()
        price_key = f'{price:.12g}'
        with self._lock:
            row = self._books.setdefault(asset_id, {'bids': {}, 'asks': {}, 'updated_at_utc': _now()})
            bid_map = row.setdefault('bids', {})
            ask_map = row.setdefault('asks', {})
            target = bid_map if side == 'BUY' else ask_map
            if size <= 1e-12:
                target.pop(price_key, None)
            else:
                target[price_key] = float(size)
            row['updated_at_utc'] = _now()

    def _get_book_snapshot(self, asset_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._books.get(asset_id)
            if not isinstance(row, dict):
                return None
            bids = row.get('bids', {})
            asks = row.get('asks', {})
            if not isinstance(bids, dict) or not isinstance(asks, dict):
                return None
            bid_levels = sorted(
                [{'price': k, 'size': f'{_safe_float(v):.12g}'} for k, v in bids.items() if _safe_float(v) > 0],
                key=lambda x: _safe_float(x['price']),
                reverse=True,
            )
            ask_levels = sorted(
                [{'price': k, 'size': f'{_safe_float(v):.12g}'} for k, v in asks.items() if _safe_float(v) > 0],
                key=lambda x: _safe_float(x['price']),
            )
            return {
                'bids': bid_levels,
                'asks': ask_levels,
                'updated_at_utc': row.get('updated_at_utc', ''),
                'event_type': 'price_change',
            }

    def _emit_book(self, *, asset_id: str, book: dict[str, Any], source: str, payload: dict[str, Any]) -> None:
        if self.on_book is None:
            return
        try:
            self.on_book(asset_id, book, source, payload)
        except Exception:
            pass
