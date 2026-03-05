from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
import time
from typing import Any

if __package__ in (None, ''):
    # Allow running as: python apps/trader/run_fetcher.py
    sys.path.append(str(Path(__file__).resolve().parents[2]))

from libs.connectors.polymarket import PolymarketAPIError, PolymarketPublicClient, extract_token_ids
from libs.core.config import load_settings
from libs.core.storage import append_jsonl, utc_now_slug, write_json



def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()



def fetch_once(client: PolymarketPublicClient, market_limit: int, max_books: int) -> dict[str, Any]:
    markets = client.list_markets(limit=market_limit)

    books: dict[str, Any] = {}
    books_count = 0

    for market in markets:
        for token_id in extract_token_ids(market):
            if books_count >= max_books:
                break
            try:
                books[token_id] = client.get_orderbook(token_id)
                books_count += 1
            except PolymarketAPIError as exc:
                books[token_id] = {'error': str(exc)}
        if books_count >= max_books:
            break

    return {
        'fetched_at_utc': _now_iso(),
        'markets_count': len(markets),
        'books_count': books_count,
        'markets': markets,
        'books': books,
    }



def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Polymarket public market data fetcher (read-only).')
    parser.add_argument('--once', action='store_true', help='Fetch one snapshot and exit (default behavior).')
    parser.add_argument('--loop', action='store_true', help='Keep fetching at an interval.')
    parser.add_argument('--interval-sec', type=int, default=60, help='Polling interval when --loop is enabled.')
    parser.add_argument('--market-limit', type=int, default=None, help='Override number of markets per snapshot.')
    parser.add_argument('--max-books', type=int, default=None, help='Override max number of orderbooks per snapshot.')
    return parser.parse_args()



def main() -> int:
    args = parse_args()
    settings = load_settings()

    market_limit = args.market_limit or settings.market_limit
    max_books = args.max_books or settings.max_books

    client = PolymarketPublicClient(
        gamma_base_url=settings.gamma_base_url,
        clob_base_url=settings.clob_base_url,
        timeout_sec=settings.timeout_sec,
    )

    loop_mode = args.loop

    while True:
        snapshot = fetch_once(client, market_limit=market_limit, max_books=max_books)
        slug = utc_now_slug()

        snapshot_path = settings.output_dir / 'snapshots' / f'{slug}.json'
        log_path = settings.output_dir / 'fetch_log.jsonl'

        write_json(snapshot_path, snapshot)
        append_jsonl(
            log_path,
            {
                'fetched_at_utc': snapshot['fetched_at_utc'],
                'snapshot_file': str(snapshot_path),
                'markets_count': snapshot['markets_count'],
                'books_count': snapshot['books_count'],
            },
        )

        print(
            f"[{snapshot['fetched_at_utc']}] snapshot={snapshot_path} "
            f"markets={snapshot['markets_count']} books={snapshot['books_count']}"
        )

        if not loop_mode:
            return 0

        time.sleep(max(1, args.interval_sec))


if __name__ == '__main__':
    raise SystemExit(main())
