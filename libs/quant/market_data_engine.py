from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any, Callable
import json
import threading
import time

from libs.connectors.polymarket import extract_token_ids
from libs.quant.db import QuantDB


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _parse_str_list(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return []
        try:
            obj = json.loads(s)
            if isinstance(obj, list):
                return [str(x) for x in obj]
        except Exception:
            return [x.strip() for x in s.split(',') if x.strip()]
    return []


def _norm_outcome_name(name: str) -> str:
    s = str(name or '').strip()
    if not s:
        return ''
    low = s.lower()
    if low in {'yes', 'y'}:
        return 'Yes'
    if low in {'no', 'n'}:
        return 'No'
    return s


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

    bids = _rows(book.get('bids', []), reverse=True)
    asks = _rows(book.get('asks', []), reverse=False)
    return {'bids': bids, 'asks': asks, 'timestamp': book.get('timestamp', '')}


def _book_metrics(book: dict[str, Any], depth_levels: int = 6) -> dict[str, Any]:
    b = _normalize_book(book)
    bids = b.get('bids', [])
    asks = b.get('asks', [])
    best_bid = bids[0]['price'] if bids else 0.0
    best_ask = asks[0]['price'] if asks else 0.0
    mid = 0.0
    spread = 0.0
    if best_bid > 0 and best_ask > 0:
        mid = (best_bid + best_ask) / 2.0
        spread = max(0.0, best_ask - best_bid)
    elif best_bid > 0:
        mid = best_bid
    elif best_ask > 0:
        mid = best_ask
    depth_bid = sum(_safe_float(x.get('size', 0.0)) for x in bids[: max(1, depth_levels)])
    depth_ask = sum(_safe_float(x.get('size', 0.0)) for x in asks[: max(1, depth_levels)])
    return {
        'book': b,
        'best_bid': best_bid,
        'best_ask': best_ask,
        'mid': mid,
        'spread': spread,
        'depth_bid': depth_bid,
        'depth_ask': depth_ask,
    }


class MarketDataEngine:
    def __init__(
        self,
        *,
        client_factory: Callable[[], Any],
        db: QuantDB,
        stream: Any | None = None,
        depth_levels: int = 6,
        max_refresh_sec: float = 25.0,
    ) -> None:
        self.client_factory = client_factory
        self.db = db
        self.stream = stream
        self.depth_levels = max(1, int(depth_levels))
        self.max_refresh_sec = max(5.0, float(max_refresh_sec))
        self._lock = threading.Lock()
        self._token_to_market: dict[str, dict[str, Any]] = {}
        self._book_cache: dict[str, dict[str, Any]] = {}
        self._last_refresh_utc = ''

    def _build_market_rows(self, m: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        market_id = str(m.get('id') or m.get('condition_id') or '').strip()
        token_ids = [str(x).strip() for x in extract_token_ids(m) if str(x).strip()]
        outcomes = [_norm_outcome_name(x) for x in _parse_str_list(m.get('outcomes'))]
        prices = [_safe_float(x, -1.0) for x in _parse_str_list(m.get('outcomePrices'))]
        if len(outcomes) < len(token_ids):
            outcomes = outcomes + [f'Outcome-{i + 1}' for i in range(len(outcomes), len(token_ids))]

        market_row = {
            'market_id': market_id,
            'question': str(m.get('question') or m.get('description') or ''),
            'liquidity': _safe_float(m.get('liquidity', 0.0)),
            'volume': _safe_float(m.get('volume', m.get('volume_num', 0.0))),
            'active': bool(m.get('active', not bool(m.get('closed', False)))),
            'closed': bool(m.get('closed', False)),
            'yes_token_id': '',
            'no_token_id': '',
            'yes_outcome': 'Yes',
            'no_outcome': 'No',
            'updated_at_utc': _now_utc(),
        }
        token_rows: list[dict[str, Any]] = []
        for i, token_id in enumerate(token_ids):
            out_name = outcomes[i] if i < len(outcomes) else f'Outcome-{i+1}'
            token_rows.append(
                {
                    'market_id': market_id,
                    'token_id': token_id,
                    'outcome': out_name,
                    'market_price': prices[i] if i < len(prices) and prices[i] >= 0 else None,
                    'tick_size': _safe_float(m.get('orderPriceMinTickSize', m.get('minimum_tick_size', 0.0))),
                    'min_size': _safe_float(m.get('orderMinSize', m.get('minimum_order_size', 0.0))),
                    'question': market_row['question'],
                    'liquidity': market_row['liquidity'],
                    'updated_at_utc': market_row['updated_at_utc'],
                }
            )
            low = out_name.lower()
            if low == 'yes' and not market_row['yes_token_id']:
                market_row['yes_token_id'] = token_id
                market_row['yes_outcome'] = out_name
            elif low == 'no' and not market_row['no_token_id']:
                market_row['no_token_id'] = token_id
                market_row['no_outcome'] = out_name

        if not market_row['yes_token_id'] and len(token_rows) >= 1:
            market_row['yes_token_id'] = str(token_rows[0]['token_id'])
            market_row['yes_outcome'] = str(token_rows[0]['outcome'])
        if not market_row['no_token_id'] and len(token_rows) >= 2:
            market_row['no_token_id'] = str(token_rows[1]['token_id'])
            market_row['no_outcome'] = str(token_rows[1]['outcome'])
        return market_row, token_rows

    def _calc_yes_no_sum(self, by_market: dict[str, list[dict[str, Any]]], rows: list[dict[str, Any]]) -> None:
        pair_sum: dict[str, float] = {}
        for market_id, items in by_market.items():
            if len(items) < 2:
                continue
            yes = next((x for x in items if str(x.get('outcome', '')).lower() == 'yes'), None)
            no = next((x for x in items if str(x.get('outcome', '')).lower() == 'no'), None)
            if yes is None or no is None:
                yes = yes or items[0]
                no = no or items[1]
            y_ask = _safe_float(yes.get('best_ask', 0.0))
            n_ask = _safe_float(no.get('best_ask', 0.0))
            if y_ask > 0 and n_ask > 0:
                pair_sum[market_id] = y_ask + n_ask
        for row in rows:
            row['yes_no_sum'] = pair_sum.get(str(row.get('market_id', '')))

    def refresh(self, *, market_limit: int = 120, max_books: int = 400) -> dict[str, Any]:
        start_ts = time.monotonic()
        deadline = start_ts + self.max_refresh_sec
        c = self.client_factory()
        raw_markets = c.list_markets(limit=max(1, min(market_limit, 2000)), active=True, closed=False)
        rows = [x for x in raw_markets if isinstance(x, dict)]
        all_market_rows: list[dict[str, Any]] = []
        token_targets: list[dict[str, Any]] = []
        for row in rows:
            market_row, token_rows = self._build_market_rows(row)
            if not market_row['market_id'] or not token_rows:
                continue
            all_market_rows.append(market_row)
            token_targets.extend(token_rows)

        token_targets = token_targets[: max(1, min(max_books, 5000))]
        token_ids = [str(x.get('token_id', '')).strip() for x in token_targets if str(x.get('token_id', '')).strip()]
        token_rows: list[dict[str, Any]] = []

        def _fetch_book(item: dict[str, Any]) -> dict[str, Any] | None:
            token_id = str(item.get('token_id', '')).strip()
            if not token_id:
                return None
            try:
                cli = self.client_factory()
                raw_book = cli.get_orderbook(token_id)
            except Exception:
                return None
            if not isinstance(raw_book, dict):
                return None
            m = _book_metrics(raw_book, depth_levels=self.depth_levels)
            return {
                **item,
                'best_bid': m['best_bid'],
                'best_ask': m['best_ask'],
                'mid': m['mid'],
                'spread': m['spread'],
                'depth_bid': m['depth_bid'],
                'depth_ask': m['depth_ask'],
                'book': m['book'],
                'updated_at_utc': _now_utc(),
            }

        max_workers = max(4, min(20, len(token_targets), int(max_books // 20) + 4))
        futures = set()
        executor = ThreadPoolExecutor(max_workers=max_workers)
        try:
            for item in token_targets:
                futures.add(executor.submit(_fetch_book, item))
            while futures and time.monotonic() < deadline:
                timeout_sec = max(0.05, deadline - time.monotonic())
                done, _pending = wait(futures, timeout=timeout_sec, return_when=FIRST_COMPLETED)
                if not done:
                    continue
                for fut in done:
                    futures.discard(fut)
                    try:
                        row = fut.result()
                    except Exception:
                        row = None
                    if isinstance(row, dict):
                        token_rows.append(row)
        finally:
            for fut in list(futures):
                fut.cancel()
            executor.shutdown(wait=False, cancel_futures=True)

        by_market: dict[str, list[dict[str, Any]]] = {}
        for row in token_rows:
            by_market.setdefault(str(row.get('market_id', '')), []).append(row)
        self._calc_yes_no_sum(by_market, token_rows)

        with self._lock:
            self._book_cache = {str(r['token_id']): dict(r.get('book', {})) for r in token_rows}
            self._token_to_market = {
                str(r['token_id']): {
                    'market_id': str(r.get('market_id', '')),
                    'outcome': str(r.get('outcome', '')),
                    'question': str(r.get('question', '')),
                    'liquidity': _safe_float(r.get('liquidity', 0.0)),
                    'tick_size': r.get('tick_size'),
                    'min_size': r.get('min_size'),
                }
                for r in token_rows
            }
            self._last_refresh_utc = _now_utc()

        for m in all_market_rows:
            self.db.upsert_market(m)
        for r in token_rows:
            self.db.upsert_book(r)

        if self.stream is not None:
            try:
                self.stream.set_assets(token_ids)
                self.stream.start(assets_ids=token_ids)
            except Exception:
                pass

        return {
            'markets': len(all_market_rows),
            'tokens': len(token_rows),
            'updated_at_utc': _now_utc(),
            'elapsed_sec': max(0.0, time.monotonic() - start_ts),
            'rows': token_rows,
            'market_rows': all_market_rows,
        }

    def on_stream_book(self, asset_id: str, book: dict[str, Any], payload: dict[str, Any] | None = None) -> None:
        token_id = str(asset_id or '').strip()
        if not token_id or not isinstance(book, dict):
            return
        with self._lock:
            info = self._token_to_market.get(token_id, {})
        if not info:
            return
        m = _book_metrics(book, depth_levels=self.depth_levels)
        row = {
            'token_id': token_id,
            'market_id': str(info.get('market_id', '')),
            'outcome': str(info.get('outcome', '')),
            'best_bid': m['best_bid'],
            'best_ask': m['best_ask'],
            'mid': m['mid'],
            'spread': m['spread'],
            'depth_bid': m['depth_bid'],
            'depth_ask': m['depth_ask'],
            'yes_no_sum': None,
            'tick_size': info.get('tick_size'),
            'min_size': info.get('min_size'),
            'updated_at_utc': _now_utc(),
        }
        self.db.upsert_book(row)
        with self._lock:
            self._book_cache[token_id] = m['book']

    def get_book(self, token_id: str) -> dict[str, Any] | None:
        tid = str(token_id or '').strip()
        if not tid:
            return None
        with self._lock:
            b = self._book_cache.get(tid)
            if isinstance(b, dict):
                return dict(b)
        return None

    def token_meta(self, token_id: str) -> dict[str, Any]:
        tid = str(token_id or '').strip()
        if not tid:
            return {}
        with self._lock:
            row = self._token_to_market.get(tid, {})
            return dict(row) if isinstance(row, dict) else {}

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {
                'last_refresh_utc': self._last_refresh_utc,
                'tracked_tokens': len(self._token_to_market),
                'books_cached': len(self._book_cache),
            }
