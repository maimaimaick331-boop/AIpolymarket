from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class PolymarketAPIError(RuntimeError):
    pass


@dataclass
class PolymarketPublicClient:
    gamma_base_url: str
    clob_base_url: str
    timeout_sec: float = 10.0

    def _get_json(self, base_url: str, path: str, params: dict[str, Any] | None = None) -> Any:
        query = f"?{urlencode(params)}" if params else ''
        url = f"{base_url.rstrip('/')}/{path.lstrip('/')}" + query

        req = Request(
            url=url,
            headers={
                'Accept': 'application/json',
                'User-Agent': 'saima-polymarket-openclaw/0.1',
            },
            method='GET',
        )

        try:
            with urlopen(req, timeout=self.timeout_sec) as response:
                raw = response.read().decode('utf-8')
                return json.loads(raw)
        except Exception as exc:
            raise PolymarketAPIError(f'GET {url} failed: {exc}') from exc

    def list_markets(self, limit: int = 20, active: bool = True, closed: bool = False) -> list[dict[str, Any]]:
        payload = self._get_json(
            self.gamma_base_url,
            '/markets',
            params={
                'limit': limit,
                'active': str(active).lower(),
                'closed': str(closed).lower(),
            },
        )

        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict) and isinstance(payload.get('markets'), list):
            return payload['markets']

        raise PolymarketAPIError('Unexpected markets payload shape')

    def get_orderbook(self, token_id: str) -> dict[str, Any]:
        payload = self._get_json(
            self.clob_base_url,
            '/book',
            params={'token_id': token_id},
        )

        if not isinstance(payload, dict):
            raise PolymarketAPIError(f'Unexpected orderbook payload for token {token_id}')
        return payload



def extract_token_ids(market: dict[str, Any]) -> list[str]:
    clob_token_ids = market.get('clobTokenIds')
    if isinstance(clob_token_ids, str):
        try:
            parsed = json.loads(clob_token_ids)
            if isinstance(parsed, list):
                return [str(token_id) for token_id in parsed if token_id is not None]
        except json.JSONDecodeError:
            pass
    elif isinstance(clob_token_ids, list):
        return [str(token_id) for token_id in clob_token_ids if token_id is not None]

    tokens = market.get('tokens')
    if not isinstance(tokens, list):
        return []

    out: list[str] = []
    for token in tokens:
        if not isinstance(token, dict):
            continue
        token_id = token.get('token_id') or token.get('tokenId') or token.get('id')
        if token_id is None:
            continue
        out.append(str(token_id))
    return out
