from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import json
import sqlite3
import threading


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _parse_utc(v: Any) -> datetime | None:
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


class QuantDB:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def _open_read_conn(self) -> sqlite3.Connection:
        """Open a short-lived read connection to avoid API starvation under write-heavy loops."""
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False, isolation_level=None, timeout=3.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        schema = """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;

        CREATE TABLE IF NOT EXISTS q_market (
          market_id TEXT PRIMARY KEY,
          question TEXT NOT NULL,
          liquidity REAL NOT NULL DEFAULT 0,
          volume REAL NOT NULL DEFAULT 0,
          active INTEGER NOT NULL DEFAULT 1,
          closed INTEGER NOT NULL DEFAULT 0,
          yes_token_id TEXT NOT NULL DEFAULT '',
          no_token_id TEXT NOT NULL DEFAULT '',
          yes_outcome TEXT NOT NULL DEFAULT 'Yes',
          no_outcome TEXT NOT NULL DEFAULT 'No',
          updated_at_utc TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS q_market_book (
          token_id TEXT PRIMARY KEY,
          market_id TEXT NOT NULL,
          outcome TEXT NOT NULL DEFAULT '',
          best_bid REAL NOT NULL DEFAULT 0,
          best_ask REAL NOT NULL DEFAULT 0,
          mid REAL NOT NULL DEFAULT 0,
          spread REAL NOT NULL DEFAULT 0,
          depth_bid REAL NOT NULL DEFAULT 0,
          depth_ask REAL NOT NULL DEFAULT 0,
          yes_no_sum REAL,
          tick_size REAL,
          min_size REAL,
          updated_at_utc TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_q_market_book_market ON q_market_book(market_id);
        CREATE INDEX IF NOT EXISTS idx_q_market_book_updated ON q_market_book(updated_at_utc);

        CREATE TABLE IF NOT EXISTS q_signal (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          time_utc TEXT NOT NULL,
          strategy_id TEXT NOT NULL,
          signal_type TEXT NOT NULL,
          market_id TEXT NOT NULL,
          token_id TEXT NOT NULL,
          side TEXT NOT NULL,
          order_kind TEXT NOT NULL,
          price REAL,
          confidence REAL NOT NULL DEFAULT 0,
          score REAL NOT NULL DEFAULT 0,
          suggested_notional REAL NOT NULL DEFAULT 0,
          reason_json TEXT NOT NULL DEFAULT '{}',
          status TEXT NOT NULL DEFAULT 'new',
          status_message TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_q_signal_time ON q_signal(time_utc DESC);
        CREATE INDEX IF NOT EXISTS idx_q_signal_status ON q_signal(status);

        CREATE TABLE IF NOT EXISTS q_order (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          time_utc TEXT NOT NULL,
          mode TEXT NOT NULL,
          strategy_id TEXT NOT NULL,
          signal_id INTEGER,
          token_id TEXT NOT NULL,
          side TEXT NOT NULL,
          order_kind TEXT NOT NULL,
          order_type TEXT NOT NULL,
          order_id TEXT NOT NULL DEFAULT '',
          price REAL,
          size REAL,
          amount REAL,
          status TEXT NOT NULL DEFAULT '',
          raw_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_q_order_time ON q_order(time_utc DESC);
        CREATE INDEX IF NOT EXISTS idx_q_order_signal ON q_order(signal_id);

        CREATE TABLE IF NOT EXISTS q_fill (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          time_utc TEXT NOT NULL,
          mode TEXT NOT NULL,
          strategy_id TEXT NOT NULL,
          signal_id INTEGER,
          order_id TEXT NOT NULL DEFAULT '',
          fill_id TEXT NOT NULL DEFAULT '',
          token_id TEXT NOT NULL,
          side TEXT NOT NULL,
          price REAL NOT NULL DEFAULT 0,
          size REAL NOT NULL DEFAULT 0,
          notional REAL NOT NULL DEFAULT 0,
          fee REAL NOT NULL DEFAULT 0,
          pnl_delta REAL NOT NULL DEFAULT 0,
          raw_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_q_fill_time ON q_fill(time_utc DESC);
        CREATE INDEX IF NOT EXISTS idx_q_fill_strategy ON q_fill(strategy_id);

        CREATE TABLE IF NOT EXISTS q_event (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          time_utc TEXT NOT NULL,
          kind TEXT NOT NULL,
          message TEXT NOT NULL,
          payload_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_q_event_time ON q_event(time_utc DESC);

        CREATE TABLE IF NOT EXISTS q_ai_eval (
          market_id TEXT PRIMARY KEY,
          question TEXT NOT NULL DEFAULT '',
          probability REAL NOT NULL DEFAULT 0,
          confidence REAL NOT NULL DEFAULT 0,
          model TEXT NOT NULL DEFAULT '',
          reason TEXT NOT NULL DEFAULT '',
          news_json TEXT NOT NULL DEFAULT '[]',
          evaluated_at_utc TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_q_ai_eval_time ON q_ai_eval(evaluated_at_utc DESC);

        CREATE TABLE IF NOT EXISTS q_strategy_risk (
          strategy_id TEXT PRIMARY KEY,
          daily_date TEXT NOT NULL DEFAULT '',
          daily_pnl REAL NOT NULL DEFAULT 0,
          consecutive_losses INTEGER NOT NULL DEFAULT 0,
          size_scale REAL NOT NULL DEFAULT 1,
          paused_until_utc TEXT NOT NULL DEFAULT '',
          updated_at_utc TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS q_account_risk (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          daily_date TEXT NOT NULL DEFAULT '',
          daily_pnl REAL NOT NULL DEFAULT 0,
          trading_enabled INTEGER NOT NULL DEFAULT 1,
          stop_reason TEXT NOT NULL DEFAULT '',
          stop_until_utc TEXT NOT NULL DEFAULT '',
          updated_at_utc TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS strategies (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL DEFAULT '',
          config_json TEXT NOT NULL DEFAULT '{}',
          status TEXT NOT NULL DEFAULT 'stopped',
          created_at TEXT NOT NULL DEFAULT '',
          stopped_at TEXT NOT NULL DEFAULT '',
          stop_reason TEXT NOT NULL DEFAULT '',
          archived_at TEXT NOT NULL DEFAULT '',
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_strategies_status ON strategies(status);
        CREATE INDEX IF NOT EXISTS idx_strategies_created ON strategies(created_at DESC);

        CREATE TABLE IF NOT EXISTS strategy_signals (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          strategy_id TEXT NOT NULL,
          timestamp TEXT NOT NULL,
          signal_type TEXT NOT NULL DEFAULT '',
          source_text TEXT NOT NULL DEFAULT '',
          source_url TEXT NOT NULL DEFAULT '',
          ai_probability REAL NOT NULL DEFAULT 0,
          ai_confidence REAL NOT NULL DEFAULT 0,
          market_price REAL NOT NULL DEFAULT 0,
          deviation REAL NOT NULL DEFAULT 0,
          decision TEXT NOT NULL DEFAULT 'hold',
          decision_reason TEXT NOT NULL DEFAULT '',
          market_id TEXT NOT NULL DEFAULT '',
          token_id TEXT NOT NULL DEFAULT '',
          source_signal_id INTEGER,
          created_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_signals_sid_time ON strategy_signals(strategy_id, timestamp DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_strategy_signals_source_signal ON strategy_signals(source_signal_id);

        CREATE TABLE IF NOT EXISTS strategy_trades (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          strategy_id TEXT NOT NULL,
          timestamp TEXT NOT NULL,
          side TEXT NOT NULL DEFAULT '',
          market TEXT NOT NULL DEFAULT '',
          price REAL NOT NULL DEFAULT 0,
          quantity REAL NOT NULL DEFAULT 0,
          cost_usdc REAL NOT NULL DEFAULT 0,
          pnl REAL NOT NULL DEFAULT 0,
          signal_id INTEGER,
          decision_reason TEXT NOT NULL DEFAULT '',
          archived INTEGER NOT NULL DEFAULT 0,
          source_fill_id INTEGER,
          created_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_trades_sid_time ON strategy_trades(strategy_id, timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_strategy_trades_archived ON strategy_trades(archived, timestamp DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_strategy_trades_source_fill ON strategy_trades(source_fill_id);

        CREATE TABLE IF NOT EXISTS strategy_param_history (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          strategy_id TEXT NOT NULL,
          changed_at TEXT NOT NULL,
          changed_by TEXT NOT NULL DEFAULT 'user',
          change_json TEXT NOT NULL DEFAULT '{}',
          note TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_param_history_sid_time
          ON strategy_param_history(strategy_id, changed_at DESC);

        CREATE TABLE IF NOT EXISTS strategy_versions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          strategy_id TEXT NOT NULL,
          version_no INTEGER NOT NULL,
          label TEXT NOT NULL DEFAULT '',
          config_json TEXT NOT NULL DEFAULT '{}',
          source TEXT NOT NULL DEFAULT '',
          note TEXT NOT NULL DEFAULT '',
          created_by TEXT NOT NULL DEFAULT 'system',
          created_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_strategy_versions_sid_ver
          ON strategy_versions(strategy_id, version_no);
        CREATE INDEX IF NOT EXISTS idx_strategy_versions_sid_time
          ON strategy_versions(strategy_id, created_at DESC, id DESC);

        CREATE TABLE IF NOT EXISTS market_translations (
          market_id TEXT PRIMARY KEY,
          name_en TEXT NOT NULL DEFAULT '',
          name_zh TEXT NOT NULL DEFAULT '',
          translated_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_market_translations_time
          ON market_translations(translated_at DESC);
        """
        with self._lock:
            self._conn.executescript(schema)
            self._conn.execute(
                """
                INSERT INTO q_account_risk (id, daily_date, daily_pnl, trading_enabled, stop_reason, stop_until_utc, updated_at_utc)
                VALUES (1, '', 0, 1, '', '', ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (_now_utc(),),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def upsert_market(self, row: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO q_market (
                  market_id, question, liquidity, volume, active, closed,
                  yes_token_id, no_token_id, yes_outcome, no_outcome, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(market_id) DO UPDATE SET
                  question=excluded.question,
                  liquidity=excluded.liquidity,
                  volume=excluded.volume,
                  active=excluded.active,
                  closed=excluded.closed,
                  yes_token_id=excluded.yes_token_id,
                  no_token_id=excluded.no_token_id,
                  yes_outcome=excluded.yes_outcome,
                  no_outcome=excluded.no_outcome,
                  updated_at_utc=excluded.updated_at_utc
                """,
                (
                    str(row.get('market_id', '')),
                    str(row.get('question', '')),
                    _safe_float(row.get('liquidity', 0.0)),
                    _safe_float(row.get('volume', 0.0)),
                    1 if bool(row.get('active', True)) else 0,
                    1 if bool(row.get('closed', False)) else 0,
                    str(row.get('yes_token_id', '')),
                    str(row.get('no_token_id', '')),
                    str(row.get('yes_outcome', 'Yes')),
                    str(row.get('no_outcome', 'No')),
                    str(row.get('updated_at_utc', _now_utc())),
                ),
            )

    def upsert_book(self, row: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO q_market_book (
                  token_id, market_id, outcome, best_bid, best_ask, mid, spread,
                  depth_bid, depth_ask, yes_no_sum, tick_size, min_size, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(token_id) DO UPDATE SET
                  market_id=excluded.market_id,
                  outcome=excluded.outcome,
                  best_bid=excluded.best_bid,
                  best_ask=excluded.best_ask,
                  mid=excluded.mid,
                  spread=excluded.spread,
                  depth_bid=excluded.depth_bid,
                  depth_ask=excluded.depth_ask,
                  yes_no_sum=excluded.yes_no_sum,
                  tick_size=excluded.tick_size,
                  min_size=excluded.min_size,
                  updated_at_utc=excluded.updated_at_utc
                """,
                (
                    str(row.get('token_id', '')),
                    str(row.get('market_id', '')),
                    str(row.get('outcome', '')),
                    _safe_float(row.get('best_bid', 0.0)),
                    _safe_float(row.get('best_ask', 0.0)),
                    _safe_float(row.get('mid', 0.0)),
                    _safe_float(row.get('spread', 0.0)),
                    _safe_float(row.get('depth_bid', 0.0)),
                    _safe_float(row.get('depth_ask', 0.0)),
                    row.get('yes_no_sum'),
                    row.get('tick_size'),
                    row.get('min_size'),
                    str(row.get('updated_at_utc', _now_utc())),
                ),
            )

    def insert_signal(self, row: dict[str, Any]) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO q_signal (
                  time_utc, strategy_id, signal_type, market_id, token_id,
                  side, order_kind, price, confidence, score, suggested_notional,
                  reason_json, status, status_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(row.get('time_utc', _now_utc())),
                    str(row.get('strategy_id', '')),
                    str(row.get('signal_type', '')),
                    str(row.get('market_id', '')),
                    str(row.get('token_id', '')),
                    str(row.get('side', '')),
                    str(row.get('order_kind', 'limit')),
                    row.get('price'),
                    _safe_float(row.get('confidence', 0.0)),
                    _safe_float(row.get('score', 0.0)),
                    _safe_float(row.get('suggested_notional', 0.0)),
                    json.dumps(row.get('reason', {}), ensure_ascii=False),
                    str(row.get('status', 'new')),
                    str(row.get('status_message', '')),
                ),
            )
            return int(cur.lastrowid or 0)

    def update_signal_status(self, signal_id: int, status: str, message: str = '') -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE q_signal SET status = ?, status_message = ? WHERE id = ?",
                (status, message, int(signal_id)),
            )

    def insert_order(self, row: dict[str, Any]) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO q_order (
                  time_utc, mode, strategy_id, signal_id, token_id, side, order_kind, order_type,
                  order_id, price, size, amount, status, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(row.get('time_utc', _now_utc())),
                    str(row.get('mode', 'paper')),
                    str(row.get('strategy_id', '')),
                    row.get('signal_id'),
                    str(row.get('token_id', '')),
                    str(row.get('side', '')),
                    str(row.get('order_kind', 'limit')),
                    str(row.get('order_type', '')),
                    str(row.get('order_id', '')),
                    row.get('price'),
                    row.get('size'),
                    row.get('amount'),
                    str(row.get('status', '')),
                    json.dumps(row.get('raw', {}), ensure_ascii=False),
                ),
            )
            return int(cur.lastrowid or 0)

    def insert_fill(self, row: dict[str, Any]) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO q_fill (
                  time_utc, mode, strategy_id, signal_id, order_id, fill_id, token_id, side,
                  price, size, notional, fee, pnl_delta, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(row.get('time_utc', _now_utc())),
                    str(row.get('mode', 'paper')),
                    str(row.get('strategy_id', '')),
                    row.get('signal_id'),
                    str(row.get('order_id', '')),
                    str(row.get('fill_id', '')),
                    str(row.get('token_id', '')),
                    str(row.get('side', '')),
                    _safe_float(row.get('price', 0.0)),
                    _safe_float(row.get('size', 0.0)),
                    _safe_float(row.get('notional', 0.0)),
                    _safe_float(row.get('fee', 0.0)),
                    _safe_float(row.get('pnl_delta', 0.0)),
                    json.dumps(row.get('raw', {}), ensure_ascii=False),
                ),
            )
            return int(cur.lastrowid or 0)

    def insert_event(self, kind: str, message: str, payload: dict[str, Any] | None = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO q_event (time_utc, kind, message, payload_json) VALUES (?, ?, ?, ?)",
                (_now_utc(), str(kind), str(message), json.dumps(payload or {}, ensure_ascii=False)),
            )
            return int(cur.lastrowid or 0)

    def upsert_ai_eval(self, row: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO q_ai_eval (
                  market_id, question, probability, confidence, model, reason, news_json, evaluated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(market_id) DO UPDATE SET
                  question=excluded.question,
                  probability=excluded.probability,
                  confidence=excluded.confidence,
                  model=excluded.model,
                  reason=excluded.reason,
                  news_json=excluded.news_json,
                  evaluated_at_utc=excluded.evaluated_at_utc
                """,
                (
                    str(row.get('market_id', '')),
                    str(row.get('question', '')),
                    _safe_float(row.get('probability', 0.0)),
                    _safe_float(row.get('confidence', 0.0)),
                    str(row.get('model', '')),
                    str(row.get('reason', '')),
                    json.dumps(row.get('news', []), ensure_ascii=False),
                    str(row.get('evaluated_at_utc', _now_utc())),
                ),
            )

    def ai_eval_recent(self, market_id: str, within_sec: int) -> dict[str, Any] | None:
        row = self.fetch_one(
            "SELECT * FROM q_ai_eval WHERE market_id = ?",
            (str(market_id),),
        )
        if not row:
            return None
        ts = str(row.get('evaluated_at_utc', '')).strip()
        if not ts:
            return None
        try:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()
            if age <= max(1, int(within_sec)):
                return row
        except Exception:
            return None
        return None

    def upsert_strategy_risk(self, strategy_id: str, row: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO q_strategy_risk (
                  strategy_id, daily_date, daily_pnl, consecutive_losses, size_scale, paused_until_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(strategy_id) DO UPDATE SET
                  daily_date=excluded.daily_date,
                  daily_pnl=excluded.daily_pnl,
                  consecutive_losses=excluded.consecutive_losses,
                  size_scale=excluded.size_scale,
                  paused_until_utc=excluded.paused_until_utc,
                  updated_at_utc=excluded.updated_at_utc
                """,
                (
                    str(strategy_id),
                    str(row.get('daily_date', '')),
                    _safe_float(row.get('daily_pnl', 0.0)),
                    _safe_int(row.get('consecutive_losses', 0)),
                    _safe_float(row.get('size_scale', 1.0)),
                    str(row.get('paused_until_utc', '')),
                    str(row.get('updated_at_utc', _now_utc())),
                ),
            )

    def strategy_risk(self, strategy_id: str) -> dict[str, Any]:
        row = self.fetch_one("SELECT * FROM q_strategy_risk WHERE strategy_id = ?", (str(strategy_id),))
        return row or {
            'strategy_id': str(strategy_id),
            'daily_date': '',
            'daily_pnl': 0.0,
            'consecutive_losses': 0,
            'size_scale': 1.0,
            'paused_until_utc': '',
            'updated_at_utc': _now_utc(),
        }

    def update_account_risk(self, row: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE q_account_risk
                SET daily_date = ?, daily_pnl = ?, trading_enabled = ?, stop_reason = ?, stop_until_utc = ?, updated_at_utc = ?
                WHERE id = 1
                """,
                (
                    str(row.get('daily_date', '')),
                    _safe_float(row.get('daily_pnl', 0.0)),
                    1 if bool(row.get('trading_enabled', True)) else 0,
                    str(row.get('stop_reason', '')),
                    str(row.get('stop_until_utc', '')),
                    str(row.get('updated_at_utc', _now_utc())),
                ),
            )

    def account_risk(self) -> dict[str, Any]:
        row = self.fetch_one("SELECT * FROM q_account_risk WHERE id = 1")
        return row or {
            'id': 1,
            'daily_date': '',
            'daily_pnl': 0.0,
            'trading_enabled': 1,
            'stop_reason': '',
            'stop_until_utc': '',
            'updated_at_utc': _now_utc(),
        }

    def upsert_strategy(self, row: dict[str, Any]) -> None:
        sid = str(row.get('id', row.get('strategy_id', ''))).strip()
        if not sid:
            return
        now = _now_utc()
        created_at = str(row.get('created_at', row.get('created_at_utc', now))).strip() or now
        stopped_at = str(row.get('stopped_at', row.get('stopped_at_utc', ''))).strip()
        stop_reason = str(row.get('stop_reason', '')).strip()
        archived_at = str(row.get('archived_at', '')).strip()
        status = str(row.get('status', 'stopped')).strip().lower()
        if status not in {'running', 'paused', 'stopped', 'archived'}:
            status = 'stopped'
        if status == 'archived' and not archived_at:
            archived_at = now
        if status in {'stopped', 'archived'} and not stopped_at:
            stopped_at = now
        cfg = row.get('config_json')
        if isinstance(cfg, dict):
            cfg_json = json.dumps(cfg, ensure_ascii=False)
        else:
            cfg_json = str(cfg or '{}')

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO strategies (
                  id, name, config_json, status, created_at, stopped_at, stop_reason, archived_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  name=excluded.name,
                  config_json=excluded.config_json,
                  status=excluded.status,
                  stopped_at=excluded.stopped_at,
                  stop_reason=excluded.stop_reason,
                  archived_at=excluded.archived_at,
                  created_at=CASE WHEN strategies.created_at = '' THEN excluded.created_at ELSE strategies.created_at END,
                  updated_at=excluded.updated_at
                """,
                (
                    sid,
                    str(row.get('name', '')).strip() or sid,
                    cfg_json,
                    status,
                    created_at,
                    stopped_at,
                    stop_reason,
                    archived_at,
                    now,
                ),
            )

    def set_strategy_status(
        self,
        strategy_id: str,
        status: str,
        *,
        stop_reason: str = '',
        stopped_at: str = '',
    ) -> None:
        sid = str(strategy_id or '').strip()
        if not sid:
            return
        st = str(status or '').strip().lower()
        if st not in {'running', 'paused', 'stopped', 'archived'}:
            return
        now = _now_utc()
        stop_time = str(stopped_at or '').strip()
        if st in {'stopped', 'archived'} and not stop_time:
            stop_time = now
        with self._lock:
            self._conn.execute(
                """
                UPDATE strategies
                SET status = ?, stopped_at = ?, stop_reason = ?, archived_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    st,
                    stop_time if st in {'stopped', 'archived'} else '',
                    str(stop_reason or ''),
                    now if st == 'archived' else '',
                    now,
                    sid,
                ),
            )
            if st == 'archived':
                self._conn.execute(
                    "UPDATE strategy_trades SET archived = 1 WHERE strategy_id = ?",
                    (sid,),
                )

    def archive_strategy(self, strategy_id: str, *, stop_reason: str = 'manual_delete') -> None:
        self.set_strategy_status(
            strategy_id=strategy_id,
            status='archived',
            stop_reason=stop_reason,
            stopped_at=_now_utc(),
        )

    def get_strategy(self, strategy_id: str) -> dict[str, Any] | None:
        sid = str(strategy_id or '').strip()
        if not sid:
            return None
        return self.fetch_one("SELECT * FROM strategies WHERE id = ?", (sid,))

    def list_strategies(self, limit: int = 1000, include_archived: bool = False) -> list[dict[str, Any]]:
        n = max(1, min(limit, 10000))
        if include_archived:
            return self.fetch_all(
                "SELECT * FROM strategies ORDER BY created_at DESC, id ASC LIMIT ?",
                (n,),
            )
        return self.fetch_all(
            "SELECT * FROM strategies WHERE status <> 'archived' ORDER BY created_at DESC, id ASC LIMIT ?",
            (n,),
        )

    def insert_strategy_signal(self, row: dict[str, Any]) -> int:
        sid = str(row.get('strategy_id', '')).strip()
        if not sid:
            return 0
        ts = str(row.get('timestamp', row.get('time_utc', _now_utc()))).strip() or _now_utc()
        source_signal_id = row.get('source_signal_id')
        created_at = str(row.get('created_at', _now_utc())).strip() or _now_utc()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO strategy_signals (
                  strategy_id, timestamp, signal_type, source_text, source_url, ai_probability,
                  ai_confidence, market_price, deviation, decision, decision_reason,
                  market_id, token_id, source_signal_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_signal_id) DO UPDATE SET
                  strategy_id=excluded.strategy_id,
                  timestamp=excluded.timestamp,
                  signal_type=excluded.signal_type,
                  source_text=excluded.source_text,
                  source_url=excluded.source_url,
                  ai_probability=excluded.ai_probability,
                  ai_confidence=excluded.ai_confidence,
                  market_price=excluded.market_price,
                  deviation=excluded.deviation,
                  decision=excluded.decision,
                  decision_reason=excluded.decision_reason,
                  market_id=excluded.market_id,
                  token_id=excluded.token_id
                """,
                (
                    sid,
                    ts,
                    str(row.get('signal_type', '')).strip(),
                    str(row.get('source_text', '')).strip(),
                    str(row.get('source_url', '')).strip(),
                    _safe_float(row.get('ai_probability', 0.0)),
                    _safe_float(row.get('ai_confidence', 0.0)),
                    _safe_float(row.get('market_price', 0.0)),
                    _safe_float(row.get('deviation', 0.0)),
                    str(row.get('decision', 'hold')).strip().lower() or 'hold',
                    str(row.get('decision_reason', '')).strip(),
                    str(row.get('market_id', '')).strip(),
                    str(row.get('token_id', '')).strip(),
                    source_signal_id,
                    created_at,
                ),
            )
            if source_signal_id is not None:
                got = self._conn.execute(
                    "SELECT id FROM strategy_signals WHERE source_signal_id = ? LIMIT 1",
                    (source_signal_id,),
                ).fetchone()
                return int(got['id']) if got is not None else 0
            return int(self._conn.execute("SELECT last_insert_rowid() AS rid").fetchone()['rid'])

    def list_strategy_signals(self, strategy_id: str, limit: int = 300) -> list[dict[str, Any]]:
        sid = str(strategy_id or '').strip()
        if not sid:
            return []
        n = max(1, min(limit, 5000))
        return self.fetch_all(
            "SELECT * FROM strategy_signals WHERE strategy_id = ? ORDER BY timestamp DESC, id DESC LIMIT ?",
            (sid, n),
        )

    def insert_strategy_trade(self, row: dict[str, Any]) -> int:
        sid = str(row.get('strategy_id', '')).strip()
        if not sid:
            return 0
        ts = str(row.get('timestamp', row.get('time_utc', _now_utc()))).strip() or _now_utc()
        source_fill_id = row.get('source_fill_id')
        created_at = str(row.get('created_at', _now_utc())).strip() or _now_utc()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO strategy_trades (
                  strategy_id, timestamp, side, market, price, quantity, cost_usdc, pnl, signal_id,
                  decision_reason, archived, source_fill_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_fill_id) DO UPDATE SET
                  strategy_id=excluded.strategy_id,
                  timestamp=excluded.timestamp,
                  side=excluded.side,
                  market=excluded.market,
                  price=excluded.price,
                  quantity=excluded.quantity,
                  cost_usdc=excluded.cost_usdc,
                  pnl=excluded.pnl,
                  signal_id=excluded.signal_id,
                  decision_reason=excluded.decision_reason,
                  archived=excluded.archived
                """,
                (
                    sid,
                    ts,
                    str(row.get('side', '')).strip().lower(),
                    str(row.get('market', '')).strip(),
                    _safe_float(row.get('price', 0.0)),
                    _safe_float(row.get('quantity', 0.0)),
                    _safe_float(row.get('cost_usdc', 0.0)),
                    _safe_float(row.get('pnl', 0.0)),
                    row.get('signal_id'),
                    str(row.get('decision_reason', '')).strip(),
                    1 if bool(row.get('archived', False)) else 0,
                    source_fill_id,
                    created_at,
                ),
            )
            if source_fill_id is not None:
                got = self._conn.execute(
                    "SELECT id FROM strategy_trades WHERE source_fill_id = ? LIMIT 1",
                    (source_fill_id,),
                ).fetchone()
                return int(got['id']) if got is not None else 0
            return int(self._conn.execute("SELECT last_insert_rowid() AS rid").fetchone()['rid'])

    def list_strategy_trades(
        self,
        strategy_id: str,
        *,
        limit: int = 300,
        include_archived: bool = True,
    ) -> list[dict[str, Any]]:
        sid = str(strategy_id or '').strip()
        if not sid:
            return []
        n = max(1, min(limit, 10000))
        if include_archived:
            return self.fetch_all(
                "SELECT * FROM strategy_trades WHERE strategy_id = ? ORDER BY timestamp DESC, id DESC LIMIT ?",
                (sid, n),
            )
        return self.fetch_all(
            "SELECT * FROM strategy_trades WHERE strategy_id = ? AND archived = 0 ORDER BY timestamp DESC, id DESC LIMIT ?",
            (sid, n),
        )

    def list_archived_strategies(self, limit: int = 500) -> list[dict[str, Any]]:
        n = max(1, min(limit, 5000))
        return self.fetch_all(
            "SELECT * FROM strategies WHERE status = 'archived' ORDER BY archived_at DESC, updated_at DESC LIMIT ?",
            (n,),
        )

    def list_archived_trades(self, strategy_id: str = '', limit: int = 1000) -> list[dict[str, Any]]:
        n = max(1, min(limit, 20000))
        sid = str(strategy_id or '').strip()
        if sid:
            return self.fetch_all(
                "SELECT * FROM strategy_trades WHERE archived = 1 AND strategy_id = ? ORDER BY timestamp DESC, id DESC LIMIT ?",
                (sid, n),
            )
        return self.fetch_all(
            "SELECT * FROM strategy_trades WHERE archived = 1 ORDER BY timestamp DESC, id DESC LIMIT ?",
            (n,),
        )

    def insert_param_history(
        self,
        *,
        strategy_id: str,
        change: dict[str, Any],
        note: str = '',
        changed_by: str = 'user',
        changed_at: str = '',
    ) -> int:
        sid = str(strategy_id or '').strip()
        if not sid:
            return 0
        ts = str(changed_at or _now_utc()).strip() or _now_utc()
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO strategy_param_history (strategy_id, changed_at, changed_by, change_json, note)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    sid,
                    ts,
                    str(changed_by or 'user'),
                    json.dumps(change or {}, ensure_ascii=False),
                    str(note or ''),
                ),
            )
            return int(cur.lastrowid or 0)

    def list_param_history(self, strategy_id: str, limit: int = 200) -> list[dict[str, Any]]:
        sid = str(strategy_id or '').strip()
        if not sid:
            return []
        n = max(1, min(limit, 5000))
        return self.fetch_all(
            "SELECT * FROM strategy_param_history WHERE strategy_id = ? ORDER BY changed_at DESC, id DESC LIMIT ?",
            (sid, n),
        )

    def insert_strategy_version(
        self,
        *,
        strategy_id: str,
        config: dict[str, Any] | list[Any] | str,
        note: str = '',
        created_by: str = 'system',
        label: str = '',
        source: str = '',
        created_at: str = '',
    ) -> dict[str, Any]:
        sid = str(strategy_id or '').strip()
        if not sid:
            return {'id': 0, 'strategy_id': '', 'version_no': 0, 'created_at': ''}
        ts = str(created_at or _now_utc()).strip() or _now_utc()
        if isinstance(config, (dict, list)):
            cfg_json = json.dumps(config, ensure_ascii=False)
        else:
            cfg_json = str(config or '{}')
        with self._lock:
            nxt_row = self._conn.execute(
                "SELECT COALESCE(MAX(version_no), 0) + 1 AS next_no FROM strategy_versions WHERE strategy_id = ?",
                (sid,),
            ).fetchone()
            version_no = int(nxt_row['next_no']) if nxt_row is not None else 1
            cur = self._conn.execute(
                """
                INSERT INTO strategy_versions (
                  strategy_id, version_no, label, config_json, source, note, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sid,
                    version_no,
                    str(label or ''),
                    cfg_json,
                    str(source or ''),
                    str(note or ''),
                    str(created_by or 'system'),
                    ts,
                ),
            )
            rid = int(cur.lastrowid or 0)
        return {'id': rid, 'strategy_id': sid, 'version_no': version_no, 'created_at': ts}

    def list_strategy_versions(self, strategy_id: str, limit: int = 120) -> list[dict[str, Any]]:
        sid = str(strategy_id or '').strip()
        if not sid:
            return []
        n = max(1, min(limit, 1000))
        return self.fetch_all(
            "SELECT * FROM strategy_versions WHERE strategy_id = ? ORDER BY version_no DESC, id DESC LIMIT ?",
            (sid, n),
        )

    def get_strategy_version(self, strategy_id: str, version_no: int) -> dict[str, Any] | None:
        sid = str(strategy_id or '').strip()
        if not sid:
            return None
        vno = int(version_no)
        if vno <= 0:
            return None
        return self.fetch_one(
            "SELECT * FROM strategy_versions WHERE strategy_id = ? AND version_no = ? LIMIT 1",
            (sid, vno),
        )

    def upsert_market_translation(
        self,
        *,
        market_id: str,
        name_en: str,
        name_zh: str,
        translated_at: str = '',
    ) -> None:
        mid = str(market_id or '').strip()
        if not mid:
            return
        ts = str(translated_at or _now_utc()).strip() or _now_utc()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO market_translations (market_id, name_en, name_zh, translated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(market_id) DO UPDATE SET
                  name_en=excluded.name_en,
                  name_zh=excluded.name_zh,
                  translated_at=excluded.translated_at
                """,
                (
                    mid,
                    str(name_en or '').strip(),
                    str(name_zh or '').strip(),
                    ts,
                ),
            )

    def get_market_translation(self, market_id: str) -> dict[str, Any] | None:
        mid = str(market_id or '').strip()
        if not mid:
            return None
        return self.fetch_one("SELECT * FROM market_translations WHERE market_id = ? LIMIT 1", (mid,))

    def get_market_translations(self, market_ids: list[str]) -> dict[str, dict[str, Any]]:
        ids = [str(x).strip() for x in market_ids if str(x).strip()]
        if not ids:
            return {}
        uniq = list(dict.fromkeys(ids))
        placeholders = ','.join(['?'] * len(uniq))
        rows = self.fetch_all(
            f"SELECT * FROM market_translations WHERE market_id IN ({placeholders})",
            tuple(uniq),
        )
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            mid = str(row.get('market_id', '')).strip()
            if not mid:
                continue
            out[mid] = row
        return out

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self._open_read_conn() as conn:
            cur = conn.execute(sql, params)
            row = cur.fetchone()
        if row is None:
            return None
        return {k: row[k] for k in row.keys()}

    def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._open_read_conn() as conn:
            cur = conn.execute(sql, params)
            rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append({k: row[k] for k in row.keys()})
        return out

    def list_markets(self, limit: int = 100) -> list[dict[str, Any]]:
        n = max(1, min(limit, 2000))
        return self.fetch_all(
            """
            SELECT m.*, y.best_bid AS yes_best_bid, y.best_ask AS yes_best_ask, y.mid AS yes_mid, y.spread AS yes_spread,
                   y.depth_bid AS yes_depth_bid, y.depth_ask AS yes_depth_ask, y.yes_no_sum AS yes_no_sum,
                   n.best_bid AS no_best_bid, n.best_ask AS no_best_ask, n.mid AS no_mid, n.spread AS no_spread,
                   n.depth_bid AS no_depth_bid, n.depth_ask AS no_depth_ask
            FROM q_market m
            LEFT JOIN q_market_book y ON y.token_id = m.yes_token_id
            LEFT JOIN q_market_book n ON n.token_id = m.no_token_id
            ORDER BY m.liquidity DESC, m.updated_at_utc DESC
            LIMIT ?
            """,
            (n,),
        )

    def list_books(self, limit: int = 200) -> list[dict[str, Any]]:
        n = max(1, min(limit, 4000))
        return self.fetch_all(
            "SELECT * FROM q_market_book ORDER BY updated_at_utc DESC LIMIT ?",
            (n,),
        )

    def list_signals(self, limit: int = 300) -> list[dict[str, Any]]:
        n = max(1, min(limit, 5000))
        return self.fetch_all(
            "SELECT * FROM q_signal ORDER BY id DESC LIMIT ?",
            (n,),
        )

    def list_orders(self, limit: int = 300) -> list[dict[str, Any]]:
        n = max(1, min(limit, 5000))
        return self.fetch_all("SELECT * FROM q_order ORDER BY id DESC LIMIT ?", (n,))

    def list_fills(self, limit: int = 300) -> list[dict[str, Any]]:
        n = max(1, min(limit, 5000))
        return self.fetch_all("SELECT * FROM q_fill ORDER BY id DESC LIMIT ?", (n,))

    def list_events(self, limit: int = 300) -> list[dict[str, Any]]:
        n = max(1, min(limit, 5000))
        return self.fetch_all("SELECT * FROM q_event ORDER BY id DESC LIMIT ?", (n,))

    def list_strategy_risk(self) -> list[dict[str, Any]]:
        return self.fetch_all("SELECT * FROM q_strategy_risk ORDER BY strategy_id ASC")

    def strategy_performance(self, *, mode: str = 'paper', hours: int = 0) -> list[dict[str, Any]]:
        mode_norm = str(mode or 'paper').strip().lower()
        where_sql = "mode = ?"
        params: list[Any] = [mode_norm]
        if int(hours) > 0:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours)))).isoformat()
            where_sql += " AND time_utc >= ?"
            params.append(cutoff)

        rows = self.fetch_all(
            f"""
            SELECT
              strategy_id,
              COUNT(1) AS fills_count,
              SUM(CASE WHEN pnl_delta > 0 THEN 1 ELSE 0 END) AS wins,
              SUM(notional) AS turnover,
              SUM(fee) AS fees,
              SUM(pnl_delta) AS pnl_total,
              MIN(time_utc) AS first_fill_utc,
              MAX(time_utc) AS last_fill_utc
            FROM q_fill
            WHERE {where_sql}
            GROUP BY strategy_id
            ORDER BY pnl_total DESC, fills_count DESC, strategy_id ASC
            """,
            tuple(params),
        )

        out: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)
        for row in rows:
            fills_count = _safe_int(row.get('fills_count', 0))
            wins = _safe_int(row.get('wins', 0))
            first_fill = _parse_utc(row.get('first_fill_utc', ''))
            runtime_hours = 0.0
            if first_fill is not None:
                runtime_hours = max(0.0, (now - first_fill).total_seconds() / 3600.0)
            out.append(
                {
                    'strategy_id': str(row.get('strategy_id', '')),
                    'mode': mode_norm,
                    'fills_count': fills_count,
                    'wins': wins,
                    'win_rate': (wins / fills_count) if fills_count > 0 else 0.0,
                    'turnover': _safe_float(row.get('turnover', 0.0)),
                    'fees': _safe_float(row.get('fees', 0.0)),
                    'pnl_total': _safe_float(row.get('pnl_total', 0.0)),
                    'first_fill_utc': str(row.get('first_fill_utc', '')),
                    'last_fill_utc': str(row.get('last_fill_utc', '')),
                    'runtime_hours': runtime_hours,
                }
            )
        return out

    def live_gate_status(
        self,
        *,
        min_hours: int = 72,
        min_win_rate: float = 0.45,
        min_pnl: float = 0.0,
        min_fills: int = 20,
        strategy_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        perf_rows = self.strategy_performance(mode='paper', hours=0)
        perf_map = {str(x.get('strategy_id', '')): x for x in perf_rows}

        ids: set[str] = set()
        for sid in perf_map.keys():
            if sid:
                ids.add(sid)
        rows = self.fetch_all("SELECT DISTINCT strategy_id FROM q_signal ORDER BY strategy_id ASC")
        for row in rows:
            sid = str(row.get('strategy_id', '')).strip()
            if sid:
                ids.add(sid)
        if strategy_ids is not None:
            ids = {str(x).strip() for x in strategy_ids if str(x).strip()}

        out_rows: list[dict[str, Any]] = []
        eligible_count = 0
        for sid in sorted(ids):
            p = perf_map.get(sid, {})
            fills_count = _safe_int(p.get('fills_count', 0))
            runtime_hours = _safe_float(p.get('runtime_hours', 0.0))
            pnl_total = _safe_float(p.get('pnl_total', 0.0))
            win_rate = _safe_float(p.get('win_rate', 0.0))
            reasons: list[str] = []
            if runtime_hours < float(min_hours):
                reasons.append(f'paper_runtime<{int(min_hours)}h')
            if fills_count < int(min_fills):
                reasons.append(f'fills<{int(min_fills)}')
            if pnl_total <= float(min_pnl):
                reasons.append(f'pnl<={float(min_pnl):.4f}')
            if win_rate < float(min_win_rate):
                reasons.append(f'win_rate<{float(min_win_rate):.2f}')
            eligible = len(reasons) == 0
            if eligible:
                eligible_count += 1
            out_rows.append(
                {
                    'strategy_id': sid,
                    'eligible': eligible,
                    'reasons': reasons,
                    'fills_count': fills_count,
                    'runtime_hours': runtime_hours,
                    'pnl_total': pnl_total,
                    'win_rate': win_rate,
                    'first_fill_utc': str(p.get('first_fill_utc', '')),
                    'last_fill_utc': str(p.get('last_fill_utc', '')),
                }
            )

        return {
            'thresholds': {
                'min_hours': int(min_hours),
                'min_fills': int(min_fills),
                'min_pnl': float(min_pnl),
                'min_win_rate': float(min_win_rate),
            },
            'count': len(out_rows),
            'eligible_count': eligible_count,
            'rows': out_rows,
            'updated_at_utc': _now_utc(),
        }

    def reset_debug_data(self, *, clear_market_translations: bool = False) -> dict[str, Any]:
        clear_tables = [
            'q_market',
            'q_market_book',
            'q_signal',
            'q_order',
            'q_fill',
            'q_event',
            'q_ai_eval',
            'q_strategy_risk',
            'q_account_risk',
            'strategies',
            'strategy_signals',
            'strategy_trades',
            'strategy_param_history',
            'strategy_versions',
        ]
        if clear_market_translations:
            clear_tables.append('market_translations')

        counts_before: dict[str, int] = {}
        with self._lock:
            for name in clear_tables:
                row = self._conn.execute(f"SELECT COUNT(1) AS c FROM {name}").fetchone()
                counts_before[name] = int(row['c']) if row is not None else 0

            self._conn.execute('BEGIN')
            try:
                for name in clear_tables:
                    self._conn.execute(f"DELETE FROM {name}")
                seq_tables = [x for x in clear_tables if x not in {'q_market', 'q_market_book', 'q_ai_eval', 'q_strategy_risk', 'q_account_risk', 'market_translations'}]
                if seq_tables:
                    placeholders = ','.join(['?'] * len(seq_tables))
                    self._conn.execute(f"DELETE FROM sqlite_sequence WHERE name IN ({placeholders})", tuple(seq_tables))
                self._conn.execute(
                    """
                    INSERT INTO q_account_risk (id, daily_date, daily_pnl, trading_enabled, stop_reason, stop_until_utc, updated_at_utc)
                    VALUES (1, '', 0, 1, '', '', ?)
                    ON CONFLICT(id) DO UPDATE SET
                      daily_date=excluded.daily_date,
                      daily_pnl=excluded.daily_pnl,
                      trading_enabled=excluded.trading_enabled,
                      stop_reason=excluded.stop_reason,
                      stop_until_utc=excluded.stop_until_utc,
                      updated_at_utc=excluded.updated_at_utc
                    """,
                    (_now_utc(),),
                )
                self._conn.execute('COMMIT')
            except Exception:
                self._conn.execute('ROLLBACK')
                raise

            counts_after: dict[str, int] = {}
            for name in clear_tables:
                row = self._conn.execute(f"SELECT COUNT(1) AS c FROM {name}").fetchone()
                counts_after[name] = int(row['c']) if row is not None else 0

        return {
            'cleared_tables': clear_tables,
            'counts_before': counts_before,
            'counts_after': counts_after,
            'updated_at_utc': _now_utc(),
        }

    def summary(self) -> dict[str, Any]:
        counts = {}
        for name in ('q_market', 'q_market_book', 'q_signal', 'q_order', 'q_fill', 'q_event', 'q_ai_eval'):
            row = self.fetch_one(f"SELECT COUNT(1) AS c FROM {name}")
            counts[name] = int((row or {}).get('c', 0))
        latest_event = self.fetch_one("SELECT * FROM q_event ORDER BY id DESC LIMIT 1") or {}
        return {
            'counts': counts,
            'latest_event': latest_event,
            'account_risk': self.account_risk(),
            'updated_at_utc': _now_utc(),
        }
