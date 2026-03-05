from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


DEFAULT_ENV_PATH = Path('.env')


def load_env_file(path: Path = DEFAULT_ENV_PATH) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue

        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    gamma_base_url: str
    clob_base_url: str
    output_dir: Path
    market_limit: int
    max_books: int
    timeout_sec: float

    paper_dir: Path
    paper_initial_cash: float
    paper_fee_bps: float
    paper_order_qty: float
    paper_hold_ticks: int
    paper_strategy: str
    paper_risk_loss_limit_pct: float

    dashboard_refresh_sec: int
    realtime_interval_sec: int
    openclaw_endpoint: str
    openclaw_timeout_sec: float
    paper_use_market_ws: bool
    market_ws_endpoint: str
    market_ws_custom_feature_enabled: bool

    live_trading_enabled: bool
    live_force_ack: bool
    live_max_order_usdc: float
    live_host: str
    live_chain_id: int
    live_signature_type: int
    live_private_key: str
    live_funder: str
    live_api_key: str
    live_api_secret: str
    live_api_passphrase: str


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'y', 'on'}


def load_settings() -> Settings:
    load_env_file()

    output_dir = Path(os.getenv('POLYMARKET_OUTPUT_DIR', 'data/raw/polymarket'))

    return Settings(
        gamma_base_url=os.getenv('POLYMARKET_GAMMA_BASE_URL', 'https://gamma-api.polymarket.com'),
        clob_base_url=os.getenv('POLYMARKET_CLOB_BASE_URL', 'https://clob.polymarket.com'),
        output_dir=output_dir,
        market_limit=int(os.getenv('POLYMARKET_MARKET_LIMIT', '20')),
        max_books=int(os.getenv('POLYMARKET_MAX_BOOKS', '20')),
        timeout_sec=float(os.getenv('POLYMARKET_TIMEOUT_SEC', '10')),
        paper_dir=Path(os.getenv('POLYMARKET_PAPER_DIR', str(output_dir / 'paper'))),
        paper_initial_cash=float(os.getenv('PAPER_INITIAL_CASH', '1000')),
        paper_fee_bps=float(os.getenv('PAPER_FEE_BPS', '2')),
        paper_order_qty=float(os.getenv('PAPER_ORDER_QTY', '10')),
        paper_hold_ticks=int(os.getenv('PAPER_HOLD_TICKS', '1')),
        paper_strategy=os.getenv('PAPER_STRATEGY', 'periodic').strip().lower(),
        paper_risk_loss_limit_pct=float(os.getenv('PAPER_RISK_LOSS_LIMIT_PCT', '3.0')),
        dashboard_refresh_sec=int(os.getenv('DASHBOARD_REFRESH_SEC', '5')),
        realtime_interval_sec=int(os.getenv('REALTIME_INTERVAL_SEC', '20')),
        openclaw_endpoint=os.getenv('OPENCLAW_ENDPOINT', ''),
        openclaw_timeout_sec=float(os.getenv('OPENCLAW_TIMEOUT_SEC', '20')),
        paper_use_market_ws=_bool_env('PAPER_USE_MARKET_WS', True),
        market_ws_endpoint=os.getenv('POLYMARKET_MARKET_WS_URL', 'wss://ws-subscriptions-clob.polymarket.com/ws/market'),
        market_ws_custom_feature_enabled=_bool_env('POLYMARKET_MARKET_WS_CUSTOM_FEATURE_ENABLED', True),
        live_trading_enabled=_bool_env('LIVE_TRADING_ENABLED', False),
        live_force_ack=_bool_env('LIVE_FORCE_ACK', False),
        live_max_order_usdc=float(os.getenv('LIVE_MAX_ORDER_USDC', '25')),
        live_host=os.getenv('POLYMARKET_LIVE_HOST', 'https://clob.polymarket.com'),
        live_chain_id=int(os.getenv('POLYMARKET_CHAIN_ID', '137')),
        live_signature_type=int(os.getenv('POLYMARKET_SIGNATURE_TYPE', '0')),
        live_private_key=os.getenv('POLYMARKET_PRIVATE_KEY', ''),
        live_funder=os.getenv('POLYMARKET_FUNDER', ''),
        live_api_key=os.getenv('POLYMARKET_API_KEY', ''),
        live_api_secret=os.getenv('POLYMARKET_API_SECRET', ''),
        live_api_passphrase=os.getenv('POLYMARKET_API_PASSPHRASE', ''),
    )
