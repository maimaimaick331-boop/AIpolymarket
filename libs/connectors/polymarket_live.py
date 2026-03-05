from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    BalanceAllowanceParams,
    MarketOrderArgs,
    OpenOrderParams,
    OrderArgs,
    OrderType,
    TradeParams,
)


class LiveClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class LiveClientConfig:
    host: str
    chain_id: int
    private_key: str
    signature_type: int
    funder: str
    api_key: str
    api_secret: str
    api_passphrase: str


class PolymarketLiveClient:
    def __init__(self, cfg: LiveClientConfig) -> None:
        self.cfg = cfg
        self._client = ClobClient(
            host=cfg.host,
            chain_id=cfg.chain_id,
            key=cfg.private_key or None,
            signature_type=cfg.signature_type,
            funder=cfg.funder or None,
        )

        if cfg.api_key and cfg.api_secret and cfg.api_passphrase:
            self._client.set_api_creds(ApiCreds(cfg.api_key, cfg.api_secret, cfg.api_passphrase))

    def _ensure_l2_auth(self) -> None:
        if not self.cfg.private_key:
            raise LiveClientError('缺少 POLYMARKET_PRIVATE_KEY，无法进行实盘鉴权。')
        if not self.cfg.funder:
            raise LiveClientError('缺少 POLYMARKET_FUNDER，无法进行实盘鉴权。')
        if not (self.cfg.api_key and self.cfg.api_secret and self.cfg.api_passphrase):
            # Try to create API key from L1 creds
            creds = self._client.create_api_key()
            self._client.set_api_creds(creds)

    def get_markets(self, next_cursor: str = 'MA==') -> Any:
        return self._client.get_markets(next_cursor=next_cursor)

    def get_order_book(self, token_id: str) -> Any:
        return self._client.get_order_book(token_id)

    def get_orders(self, market: str = '', asset_id: str = '') -> Any:
        params = OpenOrderParams(market=market or None, asset_id=asset_id or None)
        return self._client.get_orders(params=params)

    def get_trades(self, market: str = '', asset_id: str = '') -> Any:
        params = TradeParams(market=market or None, asset_id=asset_id or None)
        return self._client.get_trades(params=params)

    def get_balance(self) -> Any:
        params = BalanceAllowanceParams(signature_type=self.cfg.signature_type)
        return self._client.get_balance_allowance(params=params)

    def place_limit_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        order_type: str = 'GTC',
    ) -> Any:
        self._ensure_l2_auth()

        side_int = self._normalize_side(side)
        args = OrderArgs(token_id=token_id, side=str(side_int), price=float(price), size=float(size))
        signed = self._client.create_order(args)
        ot = self._normalize_order_type(order_type)
        return self._client.post_order(signed, orderType=ot)

    def place_market_order(self, token_id: str, side: str, amount: float, order_type: str = 'FOK') -> Any:
        self._ensure_l2_auth()

        side_int = self._normalize_side(side)
        ot = self._normalize_order_type(order_type)
        args = MarketOrderArgs(token_id=token_id, side=str(side_int), amount=float(amount), order_type=ot)
        signed = self._client.create_market_order(args)
        return self._client.post_order(signed, orderType=ot)

    def cancel(self, order_id: str) -> Any:
        self._ensure_l2_auth()
        return self._client.cancel(order_id)

    def cancel_all(self) -> Any:
        self._ensure_l2_auth()
        return self._client.cancel_all()

    @staticmethod
    def _normalize_side(side: str) -> int:
        v = str(side).strip().lower()
        if v in {'buy', 'b', '0'}:
            return 0
        if v in {'sell', 's', '1'}:
            return 1
        raise LiveClientError(f'非法 side: {side}, 应为 buy/sell')

    @staticmethod
    def _normalize_order_type(order_type: str) -> str:
        v = str(order_type).strip().upper()
        if v in {'GTC', 'FOK', 'FAK', 'GTD'}:
            return getattr(OrderType, v)
        raise LiveClientError(f'非法 order_type: {order_type}')
