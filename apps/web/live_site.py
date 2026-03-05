from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import csv
import hashlib
import io
import json
import queue
import re
import shutil
import threading
import time
import uuid
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from libs.connectors.polymarket import PolymarketPublicClient, extract_token_ids
from libs.connectors.polymarket_live import LiveClientConfig, LiveClientError, PolymarketLiveClient
from libs.core.config import load_settings
from libs.services.live_strategy_service import (
    LiveStrategyStore,
    StrategyConfig,
    generate_model_strategies,
    generate_template_strategies,
)
from libs.services.live_bot import LiveBotManager
from libs.services.model_router import (
    ModelAllocation,
    ModelProvider,
    ModelRouterStore,
    company_presets,
    choose_provider,
    discover_local_providers,
    fetch_openai_compatible_models,
    infer_company,
    models_endpoint_from_chat,
    normalize_extra_headers,
    normalize_provider_endpoint,
)
from libs.services.openclaw_client import OpenClawClient
from libs.services.live_performance import (
    LivePerformanceService,
    PerfRow,
    filter_promotion_candidates,
    save_promotion_candidate,
)
from libs.services.paper_trading import PaperTradingEngine, PaperBotManager
from libs.services.market_stream import PolymarketMarketStream
from libs.quant import (
    ExecutionEngine,
    MarketDataEngine,
    OrchestratorConfig,
    PolymarketQuantOrchestrator,
    QuantDB,
    RiskEngine,
    StrategySignalEngine,
)


APP_ROOT = Path(__file__).resolve().parent
STATIC_DIR = APP_ROOT / 'static'
settings = load_settings()

strategy_store = LiveStrategyStore(settings.paper_dir / 'live')
model_router_store = ModelRouterStore(settings.paper_dir / 'live')
paper_markets_cache_file = settings.paper_dir / 'live' / 'paper_markets_cache.json'

generate_jobs_lock = threading.Lock()
generate_jobs: dict[str, dict[str, Any]] = {}
generate_jobs_order: list[str] = []
generate_jobs_max = 120


def _save_paper_markets_cache(rows: list[dict[str, Any]], source: str) -> None:
    try:
        paper_markets_cache_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            'updated_at_utc': datetime.now(timezone.utc).isoformat(),
            'source': source,
            'count': len(rows),
            'rows': rows,
        }
        paper_markets_cache_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        return


def _load_paper_markets_cache() -> dict[str, Any]:
    if not paper_markets_cache_file.exists():
        return {}
    try:
        payload = json.loads(paper_markets_cache_file.read_text(encoding='utf-8'))
        if isinstance(payload, dict):
            return payload
    except Exception:
        return {}
    return {}


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_trim_locked() -> None:
    while len(generate_jobs_order) > generate_jobs_max:
        old_id = generate_jobs_order.pop(0)
        generate_jobs.pop(old_id, None)


def _job_set(
    job_id: str,
    *,
    status: str | None = None,
    stage: str | None = None,
    progress_pct: int | None = None,
    message: str | None = None,
    error: Any | None = None,
    result: dict[str, Any] | None = None,
    event: str = '',
) -> None:
    now = _now_utc_iso()
    with generate_jobs_lock:
        row = generate_jobs.get(job_id)
        if not isinstance(row, dict):
            return
        if status is not None:
            row['status'] = status
        if stage is not None:
            row['stage'] = stage
        if progress_pct is not None:
            row['progress_pct'] = max(0, min(100, int(progress_pct)))
        if message is not None:
            row['message'] = message
        if error is not None:
            row['error'] = error
        if result is not None:
            row['result'] = result
        row['updated_at_utc'] = now
        if event:
            rows = row.get('events', [])
            if not isinstance(rows, list):
                rows = []
            rows.append(
                {
                    'time_utc': now,
                    'stage': row.get('stage', ''),
                    'progress_pct': row.get('progress_pct', 0),
                    'message': event,
                }
            )
            row['events'] = rows[-80:]


def _job_create(payload: 'StrategyGenerateIn') -> dict[str, Any]:
    job_id = f"gen-{uuid.uuid4().hex[:12]}"
    row = {
        'job_id': job_id,
        'status': 'queued',
        'stage': 'queued',
        'progress_pct': 0,
        'message': '任务已排队',
        'request': {
            'count': int(payload.count),
            'provider_id': str(payload.provider_id or ''),
            'seed': int(payload.seed),
            'prompt': str(payload.prompt or '')[:500],
            'allow_fallback': bool(payload.allow_fallback),
        },
        'error': None,
        'result': None,
        'events': [
            {
                'time_utc': _now_utc_iso(),
                'stage': 'queued',
                'progress_pct': 0,
                'message': '任务已创建',
            }
        ],
        'created_at_utc': _now_utc_iso(),
        'updated_at_utc': _now_utc_iso(),
    }
    with generate_jobs_lock:
        generate_jobs[job_id] = row
        generate_jobs_order.append(job_id)
        _job_trim_locked()
    return row


def _job_get(job_id: str) -> dict[str, Any] | None:
    with generate_jobs_lock:
        row = generate_jobs.get(job_id)
        if not isinstance(row, dict):
            return None
        return json.loads(json.dumps(row, ensure_ascii=False))


class PaperAutoQuantManager:
    def __init__(
        self,
        *,
        strategy_store: LiveStrategyStore,
        paper_engine: PaperTradingEngine,
        paper_bot_manager: PaperBotManager,
        market_stream: PolymarketMarketStream,
    ) -> None:
        self.strategy_store = strategy_store
        self.paper_engine = paper_engine
        self.paper_bot_manager = paper_bot_manager
        self.market_stream = market_stream
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._logs: list[dict[str, Any]] = []
        self._status: dict[str, Any] = {
            'running': False,
            'phase': 'idle',
            'cycle': 0,
            'started_at_utc': '',
            'updated_at_utc': '',
            'last_cycle_at_utc': '',
            'last_error': '',
            'active_token_id': '',
            'active_strategy_id': '',
            'config': {},
            'last_result': {},
        }

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _safe_copy(self, obj: Any) -> Any:
        return json.loads(json.dumps(obj, ensure_ascii=False))

    def _set_status(self, **kwargs: Any) -> None:
        with self._lock:
            for k, v in kwargs.items():
                self._status[k] = v
            self._status['updated_at_utc'] = self._now()

    def _append_log(self, kind: str, message: str, **extra: Any) -> None:
        row = {'time_utc': self._now(), 'kind': kind, 'message': message, **extra}
        with self._lock:
            self._logs.append(row)
            if len(self._logs) > 800:
                self._logs = self._logs[-800:]
            self._status['updated_at_utc'] = row['time_utc']
        self.strategy_store.append_log({'kind': f'auto_quant_{kind}', 'message': message, **extra})

    def status(self, limit_logs: int = 200) -> dict[str, Any]:
        with self._lock:
            out = self._safe_copy(self._status)
            logs = self._safe_copy(self._logs[-max(1, min(limit_logs, 1000)) :])
        out['logs'] = logs
        out['logs_count'] = len(logs)
        return out

    @staticmethod
    def _pick_token(rows: list[dict[str, Any]]) -> dict[str, Any]:
        ranked = sorted(rows, key=lambda x: _safe_float(x.get('liquidity', 0.0)), reverse=True)
        for row in ranked:
            outcomes = row.get('outcomes', [])
            if isinstance(outcomes, list):
                for oc in outcomes:
                    if not isinstance(oc, dict):
                        continue
                    tid = str(oc.get('token_id', '')).strip()
                    px = _safe_float(oc.get('price'), default=-1.0)
                    if not tid:
                        continue
                    if px > 0 and (px <= 0.01 or px >= 0.99):
                        continue
                    return {
                        'token_id': tid,
                        'outcome': str(oc.get('outcome', '')),
                        'question': str(row.get('question', '')),
                        'market_id': str(row.get('id', '')),
                        'liquidity': _safe_float(row.get('liquidity', 0.0)),
                    }
            token_ids = row.get('token_ids', [])
            if isinstance(token_ids, list):
                for tid in token_ids:
                    s = str(tid).strip()
                    if s:
                        return {
                            'token_id': s,
                            'outcome': '',
                            'question': str(row.get('question', '')),
                            'market_id': str(row.get('id', '')),
                            'liquidity': _safe_float(row.get('liquidity', 0.0)),
                        }
        return {}

    def _enable_strategies(self, generated: list[dict[str, Any]], only_one: bool) -> str:
        ids = [str(x.get('strategy_id', '')).strip() for x in generated if isinstance(x, dict)]
        ids = [x for x in ids if x]
        if not ids:
            return ''
        target = ids[0]
        rows = self.strategy_store.load_strategies()
        updated: list[StrategyConfig] = []
        for row in rows:
            sid = str(row.strategy_id)
            if only_one:
                enabled = sid == target
            else:
                enabled = sid in ids
            updated.append(
                StrategyConfig(
                    strategy_id=row.strategy_id,
                    name=row.name,
                    strategy_type=row.strategy_type,
                    params=row.params,
                    enabled=enabled,
                    source=row.source,
                    created_at_utc=row.created_at_utc,
                )
            )
        self.strategy_store.save_strategies(updated)
        self.strategy_store.append_log({'kind': 'auto_quant_enable_strategies', 'target': target, 'only_one': only_one})
        return target

    def run_one_cycle(self, cfg: PaperAutoStartIn, *, stop_event: threading.Event | None = None) -> dict[str, Any]:
        stopev = stop_event if stop_event is not None else threading.Event()
        self._set_status(phase='preparing', last_error='')
        self._append_log('cycle_start', '自动量化单轮开始')

        payload = StrategyGenerateIn(
            count=cfg.count,
            seed=int(datetime.now(timezone.utc).timestamp()) % 1000000,
            provider_id=cfg.provider_id,
            prompt=cfg.prompt,
            allow_fallback=cfg.allow_fallback,
        )

        result = _generate_strategies_impl(
            payload,
            progress_hook=lambda stage, pct, message: self._append_log(
                'generate',
                message,
                stage=stage,
                progress_pct=pct,
            ),
        )
        generated_rows = result.get('rows', []) if isinstance(result, dict) else []
        if not isinstance(generated_rows, list) or not generated_rows:
            raise RuntimeError('策略生成为空，无法执行自动量化循环')

        pick_payload = paper_markets(limit=40, active=True, closed=False)
        market_rows = pick_payload.get('rows', []) if isinstance(pick_payload, dict) else []
        if not isinstance(market_rows, list) or not market_rows:
            raise RuntimeError('无法获取可交易市场')
        token = self._pick_token(market_rows)
        token_id = str(token.get('token_id', '')).strip()
        if not token_id:
            raise RuntimeError('未能选出有效 token')

        selected_strategy_id = self._enable_strategies(generated_rows, only_one=cfg.only_one_strategy)
        self._set_status(
            phase='sim_running',
            active_token_id=token_id,
            active_strategy_id=selected_strategy_id,
        )
        self._append_log(
            'selected',
            '选定市场与策略',
            token_id=token_id,
            outcome=token.get('outcome', ''),
            question=token.get('question', ''),
            strategy_id=selected_strategy_id,
        )

        # ensure clean state for each cycle
        self.paper_bot_manager.stop()
        if cfg.prefer_stream and settings.paper_use_market_ws:
            self.market_stream.start(assets_ids=[token_id])
            self.market_stream.add_assets([token_id])
        self.paper_bot_manager.start(
            token_id=token_id,
            interval_sec=cfg.bot_interval_sec,
            prefer_stream=cfg.prefer_stream,
        )
        self._append_log('bot', '模拟 bot 已启动', token_id=token_id, interval_sec=cfg.bot_interval_sec)

        deadline = time.monotonic() + max(2, int(cfg.run_seconds))
        while time.monotonic() < deadline:
            if stopev.is_set():
                self._append_log('stopped', '收到停止信号，本轮提前结束')
                break
            time.sleep(1.0)

        self.paper_bot_manager.stop()
        self._append_log('bot', '模拟 bot 已停止', token_id=token_id)

        perf_rows = LivePerformanceService(self.strategy_store.read_logs(limit=5000)).compute()
        perf_dict = [asdict(x) for x in perf_rows]
        generated_ids = {str(x.get('strategy_id', '')) for x in generated_rows if isinstance(x, dict)}
        top = None
        for row in perf_dict:
            if str(row.get('strategy_id', '')) in generated_ids:
                top = row
                break
        if top is None and perf_dict:
            top = perf_dict[0]

        promotion = {'approved': False, 'file': '', 'reason': ''}
        if cfg.auto_promote and top is not None:
            qualified = filter_promotion_candidates(
                rows=[PerfRow(**top)],
                min_pnl=cfg.min_pnl,
                max_dd_pct=cfg.max_dd_pct,
                min_trades=cfg.min_trades,
                min_win_rate=cfg.min_win_rate,
            )
            if qualified:
                out = settings.paper_dir / 'live' / 'promotion_candidate_live.json'
                save_promotion_candidate(out, qualified[0], thresholds={
                    'min_pnl': cfg.min_pnl,
                    'max_dd_pct': cfg.max_dd_pct,
                    'min_trades': cfg.min_trades,
                    'min_win_rate': cfg.min_win_rate,
                })
                self.strategy_store.append_log(
                    {
                        'kind': 'auto_quant_promotion_approve',
                        'strategy_id': qualified[0].strategy_id,
                        'file': str(out),
                    }
                )
                promotion = {'approved': True, 'file': str(out), 'reason': 'qualified'}
            else:
                promotion = {'approved': False, 'file': '', 'reason': 'not_qualified'}

        summary = {
            'generated': {
                'count': int(result.get('count', 0)),
                'source': result.get('source', ''),
                'provider_id': result.get('provider_id', ''),
                'used_fallback': bool(result.get('used_fallback', False)),
            },
            'selected': {
                'token_id': token_id,
                'outcome': token.get('outcome', ''),
                'question': token.get('question', ''),
                'strategy_id': selected_strategy_id,
            },
            'top_strategy': top,
            'promotion': promotion,
            'finished_at_utc': self._now(),
        }
        self._append_log(
            'cycle_done',
            '自动量化单轮完成',
            token_id=token_id,
            strategy_id=selected_strategy_id,
            top_strategy_id=(top or {}).get('strategy_id', ''),
            top_pnl=(top or {}).get('realized_pnl', 0.0),
        )
        return summary

    def _run_loop(self, cfg: PaperAutoStartIn) -> None:
        cycle = 0
        while not self._stop.is_set():
            cycle += 1
            self._set_status(cycle=cycle, phase='cycle_start', last_cycle_at_utc=self._now())
            try:
                summary = self.run_one_cycle(cfg, stop_event=self._stop)
                self._set_status(last_result=summary, phase='cycle_done', last_error='')
            except Exception as exc:
                self._set_status(last_error=str(exc), phase='failed')
                self._append_log('error', '自动量化循环失败', error=str(exc))
            if self._stop.is_set():
                break
            wait_deadline = time.monotonic() + max(5, int(cfg.cycle_interval_sec))
            self._set_status(phase='sleeping')
            while time.monotonic() < wait_deadline and not self._stop.is_set():
                time.sleep(0.5)

        self.paper_bot_manager.stop()
        self._set_status(running=False, phase='idle')
        self._append_log('stopped', '自动量化已停止')

    def start(self, cfg: PaperAutoStartIn) -> dict[str, Any]:
        with self._lock:
            running = bool(self._thread and self._thread.is_alive() and not self._stop.is_set())
            if running:
                pass
            else:
                self._stop.clear()
                cfg_dict = cfg.model_dump()
                self._status = {
                    'running': True,
                    'phase': 'starting',
                    'cycle': 0,
                    'started_at_utc': self._now(),
                    'updated_at_utc': self._now(),
                    'last_cycle_at_utc': '',
                    'last_error': '',
                    'active_token_id': '',
                    'active_strategy_id': '',
                    'config': cfg_dict,
                    'last_result': {},
                }
                self._logs = []
                self._thread = threading.Thread(target=self._run_loop, args=(cfg,), daemon=True)

        if running:
            return {'ok': False, 'reason': 'already_running', **self.status(limit_logs=80)}

        self._append_log('start', '自动量化启动', config=cfg_dict)
        if self._thread:
            self._thread.start()
        return {'ok': True, **self.status(limit_logs=80)}

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._stop.set()
            t = self._thread
        if t and t.is_alive():
            t.join(timeout=6)
        return {'ok': True, **self.status(limit_logs=120)}


app = FastAPI(title='Polymarket Live Strategy Site', version='1.0.0')
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)
app.mount('/static', StaticFiles(directory=str(STATIC_DIR)), name='static')


@app.on_event('shutdown')
def _shutdown_background_workers() -> None:
    try:
        paper_bot_manager.stop()
    except Exception:
        pass
    try:
        quant_orchestrator.stop()
    except Exception:
        pass
    try:
        paper_auto_manager.stop()
    except Exception:
        pass
    try:
        paper_market_stream.stop()
    except Exception:
        pass
    try:
        quant_db.close()
    except Exception:
        pass


class LimitOrderIn(BaseModel):
    strategy_id: str = ''
    token_id: str
    side: str
    price: float
    size: float
    order_type: str = 'GTC'
    confirm_live: bool = False


class MarketOrderIn(BaseModel):
    strategy_id: str = ''
    token_id: str
    side: str
    amount: float
    order_type: str = 'FOK'
    confirm_live: bool = False


class StrategyGenerateIn(BaseModel):
    count: int = Field(default=6, ge=1, le=30)
    seed: int = 20260304
    provider_id: str = ''
    prompt: str = ''
    allow_fallback: bool = True


class StrategyUpdateIn(BaseModel):
    strategy_id: str
    enabled: bool


class StrategyEditIn(BaseModel):
    strategy_id: str
    name: str | None = None
    strategy_type: str | None = None
    params: dict[str, Any] | None = None
    enabled: bool | None = None


class BotStartIn(BaseModel):
    token_id: str
    interval_sec: int = Field(default=20, ge=2, le=300)


class PromotionApproveIn(BaseModel):
    strategy_id: str
    min_pnl: float = 0.0
    max_dd_pct: float = 1.5
    min_trades: int = 20
    min_win_rate: float = 0.45


class ModelConfigIn(BaseModel):
    mode: str = 'weighted'
    providers: list[dict[str, Any]] = []


class ModelProviderIn(BaseModel):
    provider_id: str
    name: str
    endpoint: str = ''
    adapter: str = 'openclaw_compatible'
    model: str = ''
    company: str = 'custom'
    api_key: str = ''
    extra_headers: dict[str, Any] = {}
    enabled: bool = True
    weight: float = 1.0
    priority: int = 100


class ModelCatalogIn(BaseModel):
    company: str = 'custom'
    endpoint: str = ''
    adapter: str = 'openai_compatible'
    api_key: str = ''
    extra_headers: dict[str, Any] = {}
    limit: int = Field(default=5000, ge=1, le=50000)


class PaperLimitOrderIn(BaseModel):
    strategy_id: str = 'manual'
    token_id: str
    side: str
    price: float = Field(gt=0.0)
    size: float = Field(gt=0.0)
    order_type: str = 'GTC'
    expire_seconds: int | None = Field(default=None, ge=1, le=86400)
    tick_size: float | None = Field(default=None, gt=0.0)
    min_size: float | None = Field(default=None, gt=0.0)
    fee_bps: float | None = Field(default=None, ge=0.0, le=1000.0)


class PaperMarketOrderIn(BaseModel):
    strategy_id: str = 'manual'
    token_id: str
    side: str
    amount: float = Field(gt=0.0)
    order_type: str = 'FOK'
    fee_bps: float | None = Field(default=None, ge=0.0, le=1000.0)


class PaperResetIn(BaseModel):
    initial_cash: float | None = None


class ResetAllDataIn(BaseModel):
    confirm: bool = False
    initial_cash: float | None = None
    clear_market_translations: bool = False


class PaperBotStartIn(BaseModel):
    token_id: str
    interval_sec: int = Field(default=12, ge=2, le=300)
    prefer_stream: bool = True


class PaperStreamStartIn(BaseModel):
    assets_ids: list[str] = []
    custom_feature_enabled: bool | None = None


class PaperStreamSubIn(BaseModel):
    assets_ids: list[str] = []


class PaperAutoStartIn(BaseModel):
    provider_id: str = ''
    prompt: str = ''
    count: int = Field(default=6, ge=1, le=30)
    allow_fallback: bool = True
    only_one_strategy: bool = True
    prefer_stream: bool = True
    bot_interval_sec: int = Field(default=12, ge=2, le=300)
    run_seconds: int = Field(default=180, ge=2, le=7200)
    cycle_interval_sec: int = Field(default=45, ge=5, le=3600)
    auto_promote: bool = False
    min_pnl: float = 0.0
    max_dd_pct: float = 1.5
    min_trades: int = 20
    min_win_rate: float = 0.45


class QuantStartIn(BaseModel):
    mode: str = 'paper'  # paper | live
    cycle_sec: int = Field(default=12, ge=2, le=300)
    market_limit: int = Field(default=120, ge=10, le=2000)
    max_books: int = Field(default=400, ge=20, le=5000)
    max_signals_per_cycle: int = Field(default=16, ge=1, le=200)
    provider_id: str = ''
    ai_prompt: str = ''
    enable_arb: bool = True
    enable_mm: bool = True
    enable_ai: bool = True
    dry_run: bool = False
    confirm_live: bool = False
    enforce_live_gate: bool = True
    live_gate_min_hours: int = Field(default=72, ge=1, le=720)
    live_gate_min_win_rate: float = Field(default=0.45, ge=0.0, le=1.0)
    live_gate_min_pnl: float = 0.0
    live_gate_min_fills: int = Field(default=20, ge=1, le=50000)

    max_order_usdc: float = Field(default=25.0, ge=1.0, le=100000.0)
    max_total_exposure_usdc: float = Field(default=500.0, ge=10.0, le=5000000.0)
    strategy_daily_loss_limit: float = Field(default=-50.0, le=0.0)
    account_daily_loss_limit: float = Field(default=-100.0, le=0.0)
    loss_streak_limit: int = Field(default=5, ge=1, le=50)
    reduced_size_scale: float = Field(default=0.5, ge=0.1, le=1.0)
    race_enabled: bool = True
    race_min_fills: int = Field(default=12, ge=1, le=10000)
    race_min_win_rate: float = Field(default=0.4, ge=0.0, le=1.0)
    race_min_pnl: float = 0.0
    race_lookback_hours: int = Field(default=24, ge=1, le=720)

    arb_buy_threshold: float = Field(default=0.96, ge=0.0, le=2.0)
    arb_sell_threshold: float = Field(default=1.04, ge=0.0, le=2.0)
    fee_buffer: float = Field(default=0.02, ge=0.0, le=1.0)
    mm_liq_min: float = Field(default=1000.0, ge=0.0, le=100000000.0)
    mm_liq_max: float = Field(default=50000.0, ge=0.0, le=100000000.0)
    mm_min_spread: float = Field(default=0.05, ge=0.0, le=1.0)
    mm_min_volume: float = Field(default=1000.0, ge=0.0, le=100000000.0)
    mm_min_depth_usdc: float = Field(default=500.0, ge=0.0, le=100000000.0)
    mm_min_market_count: int = Field(default=10, ge=1, le=200)
    mm_target_market_count: int = Field(default=12, ge=1, le=500)
    mm_max_single_side_position_usdc: float = Field(default=50.0, ge=1.0, le=10000000.0)
    mm_max_position_per_market_usdc: float = Field(default=50.0, ge=1.0, le=10000000.0)
    mm_inventory_skew_strength: float = Field(default=1.0, ge=0.0, le=10.0)
    mm_allow_short_sell: bool = False
    mm_taker_rebalance: bool = False
    ai_deviation_threshold: float = Field(default=0.10, ge=0.0, le=1.0)
    ai_min_confidence: float = Field(default=0.50, ge=0.0, le=1.0)
    ai_eval_interval_sec: int = Field(default=900, ge=60, le=86400)
    ai_max_markets_per_cycle: int = Field(default=50, ge=1, le=500)


class QuantParamUpdateIn(BaseModel):
    arb_buy_threshold: float | None = Field(default=None, ge=0.0, le=2.0)
    arb_sell_threshold: float | None = Field(default=None, ge=0.0, le=2.0)
    fee_buffer: float | None = Field(default=None, ge=0.0, le=1.0)
    mm_liq_min: float | None = Field(default=None, ge=0.0, le=100000000.0)
    mm_liq_max: float | None = Field(default=None, ge=0.0, le=100000000.0)
    mm_min_spread: float | None = Field(default=None, ge=0.0, le=1.0)
    mm_min_volume: float | None = Field(default=None, ge=0.0, le=100000000.0)
    mm_min_depth_usdc: float | None = Field(default=None, ge=0.0, le=100000000.0)
    mm_min_market_count: int | None = Field(default=None, ge=1, le=200)
    mm_target_market_count: int | None = Field(default=None, ge=1, le=500)
    mm_max_single_side_position_usdc: float | None = Field(default=None, ge=1.0, le=10000000.0)
    mm_max_position_per_market_usdc: float | None = Field(default=None, ge=1.0, le=10000000.0)
    mm_inventory_skew_strength: float | None = Field(default=None, ge=0.0, le=10.0)
    mm_allow_short_sell: bool | None = None
    mm_taker_rebalance: bool | None = None
    ai_deviation_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    ai_min_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    ai_eval_interval_sec: int | None = Field(default=None, ge=60, le=86400)
    ai_max_markets_per_cycle: int | None = Field(default=None, ge=1, le=500)
    enable_arb: bool | None = None
    enable_mm: bool | None = None
    enable_ai: bool | None = None


class LlmHealthCheckIn(BaseModel):
    provider_id: str = ''


class WorkshopMessageIn(BaseModel):
    role: str
    content: str


class WorkshopChatIn(BaseModel):
    provider_id: str = ''
    messages: list[WorkshopMessageIn] = []
    draft: dict[str, Any] | None = None


class WorkshopDeployIn(BaseModel):
    draft: dict[str, Any] = {}
    provider_id: str = ''


class StrategyParamUpdateIn(BaseModel):
    params: dict[str, Any] = {}
    note: str = ''


class StrategyRollbackIn(BaseModel):
    version_no: int = Field(default=1, ge=1)
    note: str = ''


def _live_client() -> PolymarketLiveClient:
    cfg = LiveClientConfig(
        host=settings.live_host,
        chain_id=settings.live_chain_id,
        private_key=settings.live_private_key,
        signature_type=settings.live_signature_type,
        funder=settings.live_funder,
        api_key=settings.live_api_key,
        api_secret=settings.live_api_secret,
        api_passphrase=settings.live_api_passphrase,
    )
    return PolymarketLiveClient(cfg)


def _public_client() -> PolymarketPublicClient:
    return PolymarketPublicClient(
        gamma_base_url=settings.gamma_base_url,
        clob_base_url=settings.clob_base_url,
        timeout_sec=settings.timeout_sec,
    )


bot_manager = LiveBotManager(client_factory=_live_client, strategy_store=strategy_store, max_order_usdc=settings.live_max_order_usdc)
paper_engine = PaperTradingEngine(
    store_dir=settings.paper_dir / 'live',
    initial_cash_per_strategy=settings.paper_initial_cash,
    fee_bps=settings.paper_fee_bps,
    max_order_notional=max(settings.live_max_order_usdc, settings.paper_initial_cash * 0.5),
    log_hook=strategy_store.append_log,
)
paper_bot_manager = PaperBotManager(client_factory=_public_client, strategy_store=strategy_store, paper_engine=paper_engine)


def _on_market_stream_book(asset_id: str, book: dict[str, Any], source: str, payload: dict[str, Any]) -> None:
    if not asset_id or not isinstance(book, dict):
        return
    try:
        paper_engine.on_book(token_id=asset_id, book=book, source=source)
    except Exception:
        pass
    paper_bot_manager.ingest_book(token_id=asset_id, book=book, source=source)
    try:
        quant_market_data_engine.on_stream_book(asset_id=asset_id, book=book, payload=payload)
    except Exception:
        pass


def _on_market_stream_event(payload: dict[str, Any]) -> None:
    event_type = str(payload.get('event_type') or payload.get('type') or '').strip().lower()
    if event_type != 'tick_size_change':
        return
    asset_id = str(payload.get('asset_id', '')).strip()
    if not asset_id:
        return
    tick_size = payload.get('new_tick_size')
    if tick_size is None:
        tick_size = payload.get('tick_size')
    try:
        if tick_size is not None:
            paper_engine.update_token_rule(asset_id, tick_size=float(tick_size))
            strategy_store.append_log(
                {
                    'kind': 'paper_tick_size_change',
                    'token_id': asset_id,
                    'tick_size': float(tick_size),
                }
            )
    except Exception:
        return


paper_market_stream = PolymarketMarketStream(
    endpoint=settings.market_ws_endpoint,
    custom_feature_enabled=settings.market_ws_custom_feature_enabled,
    on_book=_on_market_stream_book,
    on_event=_on_market_stream_event,
)

paper_auto_manager = PaperAutoQuantManager(
    strategy_store=strategy_store,
    paper_engine=paper_engine,
    paper_bot_manager=paper_bot_manager,
    market_stream=paper_market_stream,
)

quant_db = QuantDB(settings.paper_dir / 'live' / 'quant.db')
quant_market_data_engine = MarketDataEngine(
    client_factory=_public_client,
    db=quant_db,
    stream=paper_market_stream,
    depth_levels=20,
)


def _paper_market_rows_provider() -> list[dict[str, Any]]:
    return quant_db.list_markets(limit=2000)


def _paper_ai_eval_provider(market_id: str) -> dict[str, Any] | None:
    mid = str(market_id or '').strip()
    if not mid:
        return None
    return quant_db.fetch_one(
        "SELECT probability, confidence, reason, evaluated_at_utc FROM q_ai_eval WHERE market_id = ? LIMIT 1",
        (mid,),
    )


paper_bot_manager.set_market_data_providers(
    market_rows_provider=_paper_market_rows_provider,
    ai_eval_provider=_paper_ai_eval_provider,
)
quant_signal_engine = StrategySignalEngine(
    db=quant_db,
    router_store=model_router_store,
    event_hook=quant_db.insert_event,
    paper_engine=paper_engine,
    fee_buffer=0.02,
    mm_liq_min=1000.0,
    mm_liq_max=50000.0,
    mm_min_spread=0.05,
    mm_min_volume=1000.0,
    mm_min_depth_usdc=500.0,
    mm_min_market_count=10,
    mm_target_market_count=12,
    mm_max_single_side_position_usdc=50.0,
    mm_max_position_per_market_usdc=50.0,
    mm_inventory_skew_strength=1.0,
    mm_allow_short_sell=False,
    mm_taker_rebalance=False,
    ai_deviation_threshold=0.10,
    ai_min_confidence=0.50,
    ai_eval_interval_sec=900,
    ai_max_markets_per_cycle=50,
)
quant_risk_engine = RiskEngine(
    db=quant_db,
    paper_engine=paper_engine,
    max_order_usdc=settings.live_max_order_usdc,
    max_total_exposure_usdc=500.0,
    strategy_daily_loss_limit=-50.0,
    account_daily_loss_limit=-100.0,
    loss_streak_limit=5,
    reduced_size_scale=0.5,
    race_enabled=True,
    race_min_fills=12,
    race_min_win_rate=0.40,
    race_min_pnl=0.0,
    race_lookback_hours=24,
)
quant_execution_engine = ExecutionEngine(
    db=quant_db,
    paper_engine=paper_engine,
    market_data_engine=quant_market_data_engine,
    public_client_factory=_public_client,
    live_client_factory=_live_client,
)
quant_orchestrator = PolymarketQuantOrchestrator(
    db=quant_db,
    market_data_engine=quant_market_data_engine,
    signal_engine=quant_signal_engine,
    risk_engine=quant_risk_engine,
    execution_engine=quant_execution_engine,
)

llm_health_lock = threading.Lock()
llm_health_state: dict[str, Any] = {
    'ok': False,
    'status': 'init',
    'provider_id': '',
    'error': '尚未执行健康检查',
    'latency_ms': 0,
    'checked_at_utc': '',
}
provider_pool_lock = threading.Lock()
provider_pool_state: dict[str, Any] = {
    'current_provider_id': '',
    'mode': 'priority',
    'rows': [],
    'updated_at_utc': '',
    'reason': 'init',
}
market_translate_queue: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=5000)
market_translate_lock = threading.Lock()
market_translate_pending: set[str] = set()
market_translate_worker_count = 2
workshop_migration_lock = threading.Lock()
workshop_migration_done = False


def _provider_priority(provider_id: str) -> int:
    pid = str(provider_id or '').strip()
    if pid in {'yunwu-88033', 'yunwu-237131', 'yunwu-80033'}:
        return 10
    if pid == 'yunwu-56866':
        return 20
    return 100


def _set_provider_pool_state(row: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(row, ensure_ascii=False))
    with provider_pool_lock:
        provider_pool_state.clear()
        provider_pool_state.update(out)
    return out


def _get_provider_pool_state() -> dict[str, Any]:
    with provider_pool_lock:
        return json.loads(json.dumps(provider_pool_state, ensure_ascii=False))


def _provider_basic_health_check(provider: ModelProvider) -> dict[str, Any]:
    started = time.perf_counter()
    endpoint = normalize_provider_endpoint(
        str(provider.endpoint or ''),
        adapter=str(provider.adapter or ''),
        company=str(provider.company or ''),
    )
    if not endpoint:
        return {'ok': False, 'status': 'invalid_endpoint', 'error': 'endpoint 为空', 'latency_ms': 0}
    if str(provider.adapter or '').strip().lower() != 'openai_compatible':
        return {'ok': False, 'status': 'unsupported_adapter', 'error': f"adapter={provider.adapter}", 'latency_ms': 0}
    models_ep = models_endpoint_from_chat(endpoint)
    if not models_ep:
        return {'ok': False, 'status': 'invalid_models_endpoint', 'error': '无法推导 /v1/models', 'latency_ms': 0}
    headers = {'Accept': 'application/json'}
    key = str(provider.api_key or '').strip()
    if key:
        headers['Authorization'] = f'Bearer {key}'
    headers.update(normalize_extra_headers(provider.extra_headers or {}))
    try:
        req = Request(models_ep, headers=headers, method='GET')
        with urlopen(req, timeout=12.0) as resp:
            payload = json.loads(resp.read().decode('utf-8'))
        rows = payload.get('data', []) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            rows = []
        return {
            'ok': True,
            'status': 'ok',
            'models_count': len(rows),
            'latency_ms': int((time.perf_counter() - started) * 1000),
            'error': '',
        }
    except Exception as exc:
        return {
            'ok': False,
            'status': 'error',
            'models_count': 0,
            'latency_ms': int((time.perf_counter() - started) * 1000),
            'error': str(exc),
        }


def _enabled_providers_sorted(cfg: ModelAllocation) -> list[ModelProvider]:
    rows = [p for p in (cfg.providers or []) if bool(p.enabled) and str(p.endpoint or '').strip()]
    rows.sort(key=lambda x: (_provider_priority(x.provider_id), int(getattr(x, 'priority', 100)), str(x.provider_id or '')))
    return rows


def _recheck_provider_pool(reason: str = 'manual') -> dict[str, Any]:
    cfg = model_router_store.load()
    providers = list(cfg.providers or [])
    changed = False
    out_rows: list[dict[str, Any]] = []

    for p in providers:
        pid = str(p.provider_id or '').strip()
        if not pid:
            continue
        before_enabled = bool(p.enabled)
        desired_priority = _provider_priority(pid)
        if int(getattr(p, 'priority', 100)) != desired_priority:
            p.priority = desired_priority
            changed = True

        if pid == 'local-openai':
            if p.enabled:
                p.enabled = False
                changed = True
            out_rows.append(
                {
                    'provider_id': pid,
                    'company': str(p.company or ''),
                    'model': str(p.model or ''),
                    'endpoint': str(p.endpoint or ''),
                    'priority': int(p.priority),
                    'enabled': False,
                    'available': False,
                    'status': 'disabled',
                    'error': 'local-openai 已禁用（本地 127.0.0.1:11434 未启用）',
                    'latency_ms': 0,
                    'enabled_before': before_enabled,
                }
            )
            continue

        hc = _provider_basic_health_check(p)
        available = bool(hc.get('ok', False))
        now_enabled = available
        if bool(p.enabled) != now_enabled:
            p.enabled = now_enabled
            changed = True
        out_rows.append(
            {
                'provider_id': pid,
                'company': str(p.company or ''),
                'model': str(p.model or ''),
                'endpoint': str(p.endpoint or ''),
                'priority': int(p.priority),
                'enabled': bool(p.enabled),
                'available': available,
                'status': str(hc.get('status', 'error')),
                'error': str(hc.get('error', '')),
                'latency_ms': int(hc.get('latency_ms', 0)),
                'models_count': int(hc.get('models_count', 0)),
                'enabled_before': before_enabled,
            }
        )

    cfg.mode = 'priority'
    if changed:
        model_router_store.save(ModelAllocation(mode='priority', providers=providers))

    cfg2 = model_router_store.load()
    enabled = _enabled_providers_sorted(cfg2)
    current_provider_id = enabled[0].provider_id if enabled else ''
    state = {
        'current_provider_id': current_provider_id,
        'mode': 'priority',
        'rows': sorted(out_rows, key=lambda x: (_provider_priority(str(x.get('provider_id', ''))), int(x.get('priority', 100)), str(x.get('provider_id', '')))),
        'updated_at_utc': _now_utc_iso(),
        'reason': reason,
    }
    _set_provider_pool_state(state)
    return state


def _pick_available_provider_id(preferred_id: str = '') -> str:
    pool = _get_provider_pool_state()
    rows = pool.get('rows', []) if isinstance(pool, dict) else []
    if not isinstance(rows, list) or not rows:
        pool = _recheck_provider_pool(reason='auto_pick')
        rows = pool.get('rows', []) if isinstance(pool, dict) else []
    pref = str(preferred_id or '').strip()
    if pref:
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get('provider_id', '')).strip() == pref and bool(row.get('available', False)) and bool(row.get('enabled', False)):
                return pref
    current = str(pool.get('current_provider_id', '')).strip()
    if current:
        return current
    for row in rows:
        if not isinstance(row, dict):
            continue
        if bool(row.get('available', False)) and bool(row.get('enabled', False)):
            return str(row.get('provider_id', '')).strip()
    return ''


def _set_llm_health_state(row: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(row, ensure_ascii=False))
    with llm_health_lock:
        llm_health_state.clear()
        llm_health_state.update(out)
    return out


def _get_llm_health_state() -> dict[str, Any]:
    with llm_health_lock:
        return json.loads(json.dumps(llm_health_state, ensure_ascii=False))


def _run_llm_health_check(reason: str = 'manual', provider_id: str = '') -> dict[str, Any]:
    pick_id = _pick_available_provider_id(preferred_id=provider_id)
    if not pick_id:
        pool = _get_provider_pool_state()
        rows = pool.get('rows', []) if isinstance(pool, dict) else []
        errors: list[dict[str, Any]] = []
        for item in rows if isinstance(rows, list) else []:
            if not isinstance(item, dict):
                continue
            errors.append(
                {
                    'provider_id': str(item.get('provider_id', '')),
                    'error': str(item.get('error', '') or item.get('status', 'unavailable')),
                }
            )
        row = {
            'ok': False,
            'status': 'no_available_provider',
            'provider_id': '',
            'error': '所有 provider 不可用',
            'provider_errors': errors,
            'checked_at_utc': _now_utc_iso(),
            'latency_ms': 0,
        }
    else:
        row = quant_signal_engine.llm_health_check(provider_id=pick_id)
        row['provider_id'] = pick_id
    row['reason'] = reason
    row['provider_pool'] = _get_provider_pool_state()
    _set_llm_health_state(row)
    quant_db.insert_event('llm_health_check', 'LLM连接健康检查', row)
    return row

if settings.paper_use_market_ws:
    paper_market_stream.start(assets_ids=[])

threading.Thread(target=_recheck_provider_pool, kwargs={'reason': 'startup'}, daemon=True).start()
threading.Thread(target=_run_llm_health_check, kwargs={'reason': 'startup'}, daemon=True).start()


def _guard_live(confirm_live: bool) -> None:
    if not settings.live_trading_enabled:
        raise HTTPException(status_code=403, detail='LIVE_TRADING_ENABLED=false，禁止真实下单。')
    if settings.live_force_ack and not confirm_live:
        raise HTTPException(status_code=400, detail='实盘下单需要 confirm_live=true 二次确认。')
    if not (
        settings.live_private_key
        and settings.live_funder
        and settings.live_api_key
        and settings.live_api_secret
        and settings.live_api_passphrase
    ):
        raise HTTPException(
            status_code=400,
            detail='实盘凭证未完整配置（PRIVATE_KEY/FUNDER/API_KEY/API_SECRET/API_PASSPHRASE）。',
        )


def _check_notional(side: str, price: float | None, size: float | None, amount: float | None) -> None:
    notional = 0.0
    if amount is not None:
        notional = float(amount)
    elif price is not None and size is not None:
        notional = float(price) * float(size)
    if notional > settings.live_max_order_usdc:
        raise HTTPException(
            status_code=400,
            detail=f'下单名义金额 {notional:.4f} 超出 LIVE_MAX_ORDER_USDC={settings.live_max_order_usdc}',
        )


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _parse_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x) for x in value if x is not None and str(x).strip()]
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
        except Exception:
            return []
        if isinstance(parsed, list):
            return [str(x) for x in parsed if x is not None and str(x).strip()]
    return []


def _sort_book_side(rows: Any, descending: bool) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        price = _safe_float(row.get('price'))
        size = _safe_float(row.get('size'))
        if price <= 0 or size <= 0:
            continue
        out.append({'price': f'{price:.12g}', 'size': f'{size:.12g}'})
    out.sort(key=lambda x: _safe_float(x.get('price')), reverse=descending)
    return out


def _normalize_orderbook_payload(book: Any) -> dict[str, Any]:
    if not isinstance(book, dict):
        return {}
    out = dict(book)
    out['bids'] = _sort_book_side(book.get('bids', []), descending=True)
    out['asks'] = _sort_book_side(book.get('asks', []), descending=False)
    return out


def _mask_secret(value: str) -> str:
    s = (value or '').strip()
    if not s:
        return ''
    if len(s) <= 6:
        return '*' * len(s)
    return f'{s[:3]}***{s[-3:]}'


def _provider_public_payload(p: ModelProvider) -> dict[str, Any]:
    d = asdict(p)
    key = str(d.get('api_key', '') or '').strip()
    d['has_api_key'] = bool(key)
    d['api_key_masked'] = _mask_secret(key)
    d['api_key'] = ''
    headers = normalize_extra_headers(d.get('extra_headers', {}))
    pub_headers: dict[str, str] = {}
    for hk, hv in headers.items():
        lower = hk.strip().lower()
        if 'authorization' in lower or 'api-key' in lower or lower == 'x-api-key':
            pub_headers[hk] = _mask_secret(hv)
        else:
            pub_headers[hk] = hv
    d['extra_headers'] = pub_headers
    return d


WORKSHOP_ALLOWED_TYPES = {'ai_probability', 'arbitrage', 'market_making', 'spread_capture', 'custom'}
WORKSHOP_ALLOWED_DIRECTIONS = {'buy_yes', 'buy_no', 'both', 'market_make'}
WORKSHOP_ALLOWED_CONDITION_TYPES = {
    'spread_threshold',
    'ai_deviation',
    'arb_gap',
    'volume_filter',
    'price_range',
}
WORKSHOP_ALLOWED_OPERATORS = {'>=', '<=', '>', '<', '=='}
WORKSHOP_PLACEHOLDER_CONDITIONS = {
    'market condition satisfied',
    'condition satisfied',
    'custom condition',
}
WORKSHOP_PLACEHOLDER_PARAM_KEYS = {'custom_threshold', 'threshold', 'condition'}
WORKSHOP_DIRECTION_ALIASES = {
    '做多yes': 'buy_yes',
    'buy_yes': 'buy_yes',
    'long_yes': 'buy_yes',
    '做空yes': 'buy_no',
    'buy_no': 'buy_no',
    'long_no': 'buy_no',
    '双向': 'both',
    'both': 'both',
    'two_sided': 'both',
    '做市': 'market_make',
    'market_make': 'market_make',
    'market_making': 'market_make',
}
WORKSHOP_TYPE_ALIASES = {
    'ai_probability': 'ai_probability',
    'arbitrage': 'arbitrage',
    'market_making': 'market_making',
    'spread_capture': 'spread_capture',
    'custom': 'custom',
}


def _workshop_extract_json_object(text: str) -> dict[str, Any]:
    raw = str(text or '').strip()
    if not raw:
        return {}
    if raw.startswith('```'):
        raw = raw.replace('```json', '').replace('```', '').strip()

    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    start = raw.find('{')
    while start >= 0:
        depth = 0
        in_str = False
        escaped = False
        for idx in range(start, len(raw)):
            ch = raw[idx]
            if in_str:
                if escaped:
                    escaped = False
                    continue
                if ch == '\\':
                    escaped = True
                    continue
                if ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
                continue
            if ch == '{':
                depth += 1
                continue
            if ch == '}':
                depth -= 1
                if depth == 0:
                    chunk = raw[start : idx + 1]
                    try:
                        obj = json.loads(chunk)
                        if isinstance(obj, dict):
                            return obj
                    except Exception:
                        break
        start = raw.find('{', start + 1)
    return {}


def _workshop_extract_strategy_json_block(text: str) -> dict[str, Any]:
    raw = str(text or '')
    markers = ['```strategy_json', '```json']
    for marker in markers:
        start = raw.find(marker)
        if start < 0:
            continue
        chunk = raw[start + len(marker) :]
        end = chunk.find('```')
        if end < 0:
            continue
        body = chunk[:end].strip()
        if not body:
            continue
        try:
            obj = json.loads(body)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return _workshop_extract_json_object(raw)


def _workshop_infer_type_from_text(text: str) -> str:
    t = str(text or '').lower()
    if any(x in t for x in ['arbitrage', '套利', 'yes+no', 'yes + no']):
        return 'arbitrage'
    if any(x in t for x in ['market maker', '做市', 'spread', '双边挂单']):
        return 'market_making'
    if any(x in t for x in ['ai', 'llm', '概率', '新闻', 'confidence', '置信度']):
        return 'ai_probability'
    if any(x in t for x in ['价差捕捉', 'mean reversion', '均值回归', 'spread capture']):
        return 'spread_capture'
    return 'spread_capture'


def _workshop_normalize_direction(value: Any, default: str = 'both') -> str:
    raw = str(value or '').strip().lower()
    mapped = WORKSHOP_DIRECTION_ALIASES.get(raw, '')
    if mapped in WORKSHOP_ALLOWED_DIRECTIONS:
        return mapped
    return default if default in WORKSHOP_ALLOWED_DIRECTIONS else 'both'


def _workshop_normalize_type(value: Any, user_text: str = '') -> str:
    raw = str(value or '').strip().lower()
    mapped = WORKSHOP_TYPE_ALIASES.get(raw, '')
    if mapped in WORKSHOP_ALLOWED_TYPES:
        return mapped
    inferred = _workshop_infer_type_from_text(user_text)
    if inferred in WORKSHOP_ALLOWED_TYPES:
        return inferred
    return 'spread_capture'


def _workshop_numeric_or_default(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _workshop_parse_keywords(value: Any, default: Any = 'all') -> list[str] | str:
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() == 'all':
            return 'all'
        parts = [x.strip() for x in re.split(r'[,，\s]+', text) if x.strip()]
        return parts if parts else 'all'
    if isinstance(value, list):
        parts = [str(x).strip() for x in value if str(x).strip()]
        return parts if parts else 'all'
    return _workshop_parse_keywords(default, default='all')


def _workshop_legacy_condition_to_new(row: dict[str, Any], fallback_type: str = 'spread_threshold') -> dict[str, Any] | None:
    cond = str(row.get('condition', '')).strip()
    key = str(row.get('param_key', '')).strip().lower()
    value = row.get('default')
    if not cond and not key:
        return None
    ctype = fallback_type
    op = '>='
    if key in {'min_spread', 'spread_threshold', 'mean_rev_threshold'}:
        ctype = 'spread_threshold'
        op = '>='
    elif key in {'prob_diff_threshold', 'ai_prob_diff_threshold'}:
        ctype = 'ai_deviation'
        op = '>='
    elif key in {'confidence_threshold', 'ai_confidence_threshold'}:
        ctype = 'ai_deviation'
        op = '>='
    elif key in {'arb_buy_threshold'}:
        ctype = 'arb_gap'
        op = '>='
        try:
            value = max(0.0, 1.0 - float(value))
        except Exception:
            value = 0.04
    elif key in {'arb_sell_threshold'}:
        ctype = 'arb_gap'
        op = '>='
        try:
            value = max(0.0, float(value) - 1.0)
        except Exception:
            value = 0.04
    elif key in {'min_volume', 'min_volume_24h'}:
        ctype = 'volume_filter'
        op = '>='
    elif key in {'price_min', 'price_max'}:
        ctype = 'price_range'
        op = '<=' if key == 'price_max' else '>='
    v = _workshop_numeric_or_default(value, 0.0)
    return {
        'type': ctype,
        'operator': op,
        'value': round(v, 8),
        'description': cond or key or ctype,
    }


def _workshop_eval_operator(lhs: float, op: str, rhs: float) -> bool:
    if op == '>=':
        return lhs >= rhs
    if op == '<=':
        return lhs <= rhs
    if op == '>':
        return lhs > rhs
    if op == '<':
        return lhs < rhs
    if op == '==':
        return abs(lhs - rhs) <= 1e-9
    return False


def _workshop_default_draft(user_text: str = '') -> dict[str, Any]:
    stype = _workshop_infer_type_from_text(user_text)
    if stype == 'arbitrage':
        return {
            'name': 'YesNo 价差套利',
            'description': '监控 Yes+No 合成价格偏离 1.0，出现可覆盖手续费的偏差时执行套利。',
            'type': 'arbitrage',
            'direction': 'both',
            'trigger_conditions': [
                {'type': 'arb_gap', 'operator': '>=', 'value': 0.04, 'description': 'Yes+No 偏离 1.0 至少 4%'},
                {'type': 'volume_filter', 'operator': '>=', 'value': 1000, 'description': '24h 成交额至少 1000 USDC'},
            ],
            'position_sizing': {'per_trade_usdc': 20, 'max_total_usdc': 200},
            'risk_management': {
                'stop_loss_total': -50,
                'stop_loss_per_trade_pct': -0.08,
                'take_profit_per_trade_pct': 0.06,
                'max_consecutive_losses': 5,
            },
            'market_filter': {
                'min_volume_24h': 1000,
                'min_liquidity': 500,
                'keywords': 'all',
            },
            'check_interval_minutes': 5,
        }
    if stype == 'market_making':
        return {
            'name': 'Spread 做市捕捉',
            'description': '在价差足够且流动性达标的市场做双边报价，赚取 spread。',
            'type': 'market_making',
            'direction': 'market_make',
            'trigger_conditions': [
                {'type': 'spread_threshold', 'operator': '>=', 'value': 0.05, 'description': '盘口 spread 至少 5%'},
                {'type': 'volume_filter', 'operator': '>=', 'value': 1000, 'description': '24h 成交额至少 1000 USDC'},
            ],
            'position_sizing': {'per_trade_usdc': 15, 'max_total_usdc': 180},
            'risk_management': {
                'stop_loss_total': -45,
                'stop_loss_per_trade_pct': -0.06,
                'take_profit_per_trade_pct': 0.04,
                'max_consecutive_losses': 5,
            },
            'market_filter': {
                'min_volume_24h': 1000,
                'min_liquidity': 500,
                'keywords': 'all',
            },
            'check_interval_minutes': 3,
        }
    if stype == 'ai_probability':
        return {
            'name': 'AI 概率偏差策略',
            'description': '用 AI 概率评估与市场隐含概率对比，偏差足够大时交易。',
            'type': 'ai_probability',
            'direction': 'both',
            'trigger_conditions': [
                {'type': 'ai_deviation', 'operator': '>=', 'value': 0.10, 'description': 'AI 概率偏差至少 10%'},
                {'type': 'volume_filter', 'operator': '>=', 'value': 3000, 'description': '24h 成交额至少 3000 USDC'},
            ],
            'position_sizing': {'per_trade_usdc': 20, 'max_total_usdc': 200},
            'risk_management': {
                'stop_loss_total': -50,
                'stop_loss_per_trade_pct': -0.10,
                'take_profit_per_trade_pct': 0.15,
                'max_consecutive_losses': 5,
            },
            'market_filter': {
                'min_volume_24h': 3000,
                'min_liquidity': 800,
                'keywords': 'all',
            },
            'check_interval_minutes': 15,
        }
    if stype == 'spread_capture':
        return {
            'name': '价格回归捕捉',
            'description': '当市场价格进入设定区间时执行买入/卖出，捕捉短期回归机会。',
            'type': 'spread_capture',
            'direction': 'buy_yes',
            'trigger_conditions': [
                {'type': 'price_range', 'operator': '<=', 'value': 0.42, 'description': 'Yes 价格低于 0.42 时买入'},
                {'type': 'volume_filter', 'operator': '>=', 'value': 1000, 'description': '24h 成交额至少 1000 USDC'},
            ],
            'position_sizing': {'per_trade_usdc': 12, 'max_total_usdc': 150},
            'risk_management': {
                'stop_loss_total': -40,
                'stop_loss_per_trade_pct': -0.08,
                'take_profit_per_trade_pct': 0.10,
                'max_consecutive_losses': 5,
            },
            'market_filter': {
                'min_volume_24h': 1000,
                'min_liquidity': 500,
                'keywords': 'all',
            },
            'check_interval_minutes': 10,
        }
    return {
        'name': '通用策略模板',
        'description': '可执行的阈值条件组合策略。',
        'type': 'spread_capture',
        'direction': 'both',
        'trigger_conditions': [
            {'type': 'spread_threshold', 'operator': '>=', 'value': 0.05, 'description': 'spread 至少 5%'},
            {'type': 'volume_filter', 'operator': '>=', 'value': 1000, 'description': '24h 成交额至少 1000 USDC'},
        ],
        'position_sizing': {'per_trade_usdc': 15, 'max_total_usdc': 150},
        'risk_management': {
            'stop_loss_total': -40,
            'stop_loss_per_trade_pct': -0.08,
            'take_profit_per_trade_pct': 0.10,
            'max_consecutive_losses': 5,
        },
        'market_filter': {
            'min_volume_24h': 1000,
            'min_liquidity': 500,
            'keywords': 'all',
        },
        'check_interval_minutes': 10,
    }


def _workshop_normalize_draft(draft: Any, user_text: str = '') -> dict[str, Any]:
    base = _workshop_default_draft(user_text=user_text)
    if not isinstance(draft, dict):
        return base

    out = dict(base)
    stype = _workshop_normalize_type(draft.get('type', out.get('type', 'spread_capture')), user_text=user_text)
    out['type'] = stype

    name = str(draft.get('name', out['name'])).strip()
    out['name'] = name[:120] if name else out['name']
    desc = str(draft.get('description', out['description'])).strip()
    out['description'] = desc[:600] if desc else out['description']

    out['direction'] = _workshop_normalize_direction(draft.get('direction', out.get('direction', 'both')), default=str(out.get('direction', 'both')))

    raw_conditions = draft.get('trigger_conditions')
    cond_rows: list[dict[str, Any]] = []
    if isinstance(raw_conditions, list):
        for row in raw_conditions[:12]:
            if not isinstance(row, dict):
                continue
            if 'type' in row:
                ctype = str(row.get('type', '')).strip().lower()
                if ctype not in WORKSHOP_ALLOWED_CONDITION_TYPES:
                    continue
                op = str(row.get('operator', '')).strip()
                if op not in WORKSHOP_ALLOWED_OPERATORS:
                    op = '>='
                value = _workshop_numeric_or_default(row.get('value'), 0.0)
                desc_text = str(row.get('description', '')).strip() or ctype
                cond_rows.append(
                    {
                        'type': ctype,
                        'operator': op,
                        'value': round(value, 8),
                        'description': desc_text[:300],
                    }
                )
                continue
            legacy = _workshop_legacy_condition_to_new(row)
            if legacy:
                cond_rows.append(legacy)
    out['trigger_conditions'] = cond_rows if cond_rows else list(base.get('trigger_conditions', []))

    raw_size = draft.get('position_sizing')
    base_size = base.get('position_sizing', {})
    if not isinstance(raw_size, dict):
        raw_size = {}
    per_trade = max(
        0.1,
        _safe_float(
            raw_size.get('per_trade_usdc', raw_size.get('per_trade')),
            _safe_float(base_size.get('per_trade_usdc', base_size.get('per_trade', 20.0))),
        ),
    )
    max_total = max(
        per_trade,
        _safe_float(
            raw_size.get('max_total_usdc', raw_size.get('max_total')),
            _safe_float(base_size.get('max_total_usdc', base_size.get('max_total', 200.0))),
        ),
    )
    out['position_sizing'] = {
        'per_trade_usdc': round(per_trade, 6),
        'max_total_usdc': round(max_total, 6),
    }

    raw_risk = draft.get('risk_management')
    base_risk = base.get('risk_management', {})
    if not isinstance(raw_risk, dict):
        raw_risk = {}
    stop_total = _safe_float(raw_risk.get('stop_loss_total'), _safe_float(base_risk.get('stop_loss_total'), -50.0))
    if stop_total > 0:
        stop_total = -stop_total
    stop_trade_pct = _safe_float(
        raw_risk.get('stop_loss_per_trade_pct'),
        _safe_float(base_risk.get('stop_loss_per_trade_pct'), -0.10),
    )
    if stop_trade_pct > 0:
        stop_trade_pct = -stop_trade_pct
    tp_trade_pct = max(
        0.0,
        _safe_float(
            raw_risk.get('take_profit_per_trade_pct'),
            _safe_float(base_risk.get('take_profit_per_trade_pct'), 0.10),
        ),
    )
    out['risk_management'] = {
        'stop_loss_total': round(stop_total, 6),
        'stop_loss_per_trade_pct': round(stop_trade_pct, 6),
        'take_profit_per_trade_pct': round(tp_trade_pct, 6),
        'max_consecutive_losses': max(
            1,
            min(
                100,
                int(
                    round(
                        _safe_float(
                            raw_risk.get('max_consecutive_losses'),
                            _safe_float(base_risk.get('max_consecutive_losses'), 5),
                        )
                    )
                ),
            ),
        ),
    }

    raw_filter = draft.get('market_filter')
    base_filter = base.get('market_filter', {})
    if not isinstance(raw_filter, dict):
        raw_filter = {}
    if not raw_filter:
        legacy_target = str(draft.get('target_markets', '')).strip()
        if legacy_target:
            raw_filter = {'keywords': legacy_target}
    if not isinstance(base_filter, dict):
        base_filter = {}
    out['market_filter'] = {
        'min_volume_24h': round(
            max(
                0.0,
                _safe_float(
                    raw_filter.get('min_volume_24h'),
                    _safe_float(base_filter.get('min_volume_24h'), 0.0),
                ),
            ),
            6,
        ),
        'min_liquidity': round(
            max(
                0.0,
                _safe_float(
                    raw_filter.get('min_liquidity'),
                    _safe_float(base_filter.get('min_liquidity'), 0.0),
                ),
            ),
            6,
        ),
        'keywords': _workshop_parse_keywords(
            raw_filter.get('keywords', base_filter.get('keywords', 'all')),
            default=base_filter.get('keywords', 'all'),
        ),
    }

    interval = int(round(_safe_float(draft.get('check_interval_minutes'), _safe_float(base.get('check_interval_minutes'), 30))))
    out['check_interval_minutes'] = max(1, min(1440, interval))
    return out


def _workshop_has_placeholder_trigger(draft: dict[str, Any]) -> bool:
    rows = draft.get('trigger_conditions')
    if not isinstance(rows, list):
        return True
    if not rows:
        return True
    for row in rows:
        if not isinstance(row, dict):
            continue
        cond = str(row.get('condition', row.get('description', ''))).strip().lower()
        key = str(row.get('param_key', row.get('type', ''))).strip().lower()
        if not cond and not key:
            continue
        if cond in WORKSHOP_PLACEHOLDER_CONDITIONS:
            return True
        if key in WORKSHOP_PLACEHOLDER_PARAM_KEYS:
            return True
        if 'placeholder' in cond:
            return True
        if 'market condition satisfied' in cond:
            return True
    return False


def _workshop_force_executable_draft(draft: Any, user_text: str = '') -> dict[str, Any]:
    out = _workshop_normalize_draft(draft, user_text=user_text)
    stype = str(out.get('type', 'custom')).strip().lower()
    placeholder = _workshop_has_placeholder_trigger(out)
    text_blob = ' '.join(
        [
            str(user_text or ''),
            str(out.get('name', '')),
            str(out.get('description', '')),
        ]
    ).strip()
    inferred = _workshop_infer_type_from_text(text_blob)

    if placeholder:
        target_type = stype
        if stype == 'custom' and inferred in {'arbitrage', 'market_making', 'ai_probability', 'spread_capture'}:
            target_type = inferred
        if target_type in {'arbitrage', 'market_making', 'ai_probability', 'spread_capture'}:
            tpl = _workshop_default_draft(text_blob if text_blob else target_type)
            out['type'] = target_type
            out['direction'] = str(tpl.get('direction', out.get('direction', 'both')))
            out['trigger_conditions'] = list(tpl.get('trigger_conditions', []))
            out['market_filter'] = dict(tpl.get('market_filter', out.get('market_filter', {'keywords': 'all'})))
            desc = str(out.get('description', '')).strip().lower()
            if not desc or 'user-defined strategy' in desc or 'placeholder' in desc:
                out['description'] = str(tpl.get('description', out.get('description', '')))
        else:
            out['trigger_conditions'] = [
                {'type': 'spread_threshold', 'operator': '>=', 'value': 0.05, 'description': 'spread 至少 5%'},
                {'type': 'volume_filter', 'operator': '>=', 'value': 1000, 'description': '24h 成交额至少 1000 USDC'},
            ]
            out['description'] = '使用可执行的均值回归条件（无占位符）。'
    return _workshop_normalize_draft(out, user_text=user_text)


def _workshop_render_reply(draft: dict[str, Any], notes: list[str] | None = None) -> str:
    stype = str(draft.get('type', 'custom')).strip().lower()
    logic_map = {
        'arbitrage': '监控 Yes/No 组合定价偏离，当两边合计价格偏离 1.0 且覆盖手续费后执行配对交易。',
        'market_making': '在价差足够的市场双边挂单，利用 spread 收益并通过库存偏斜控制回到中性仓位。',
        'ai_probability': '用 AI 估计事件概率，与市场隐含概率对比，偏差足够大且置信度达标时交易。',
        'spread_capture': '在目标市场的价差/价格区间达到阈值时执行捕捉，偏向短周期高执行纪律。',
        'custom': '基于你描述的规则进行条件触发交易，并把仓位与风控参数结构化管理。',
    }
    core_logic = logic_map.get(stype, logic_map['custom'])

    cond_rows = draft.get('trigger_conditions', [])
    cond_text = []
    if isinstance(cond_rows, list):
        for row in cond_rows[:4]:
            if not isinstance(row, dict):
                continue
            c = str(row.get('description', row.get('condition', ''))).strip()
            if c:
                cond_text.append(c)
    trigger_text = '；'.join(cond_text) if cond_text else '当价格、流动性与风控阈值同时满足时触发。'

    sizing = draft.get('position_sizing', {}) if isinstance(draft.get('position_sizing'), dict) else {}
    risk = draft.get('risk_management', {}) if isinstance(draft.get('risk_management'), dict) else {}
    per_trade = _safe_float(sizing.get('per_trade_usdc', sizing.get('per_trade')), 0.0)
    max_total = _safe_float(sizing.get('max_total_usdc', sizing.get('max_total')), 0.0)
    sl_total = _safe_float(risk.get('stop_loss_total'), 0.0)
    sl_trade = _safe_float(risk.get('stop_loss_per_trade_pct'), 0.0)
    tp_trade = _safe_float(risk.get('take_profit_per_trade_pct'), 0.0)
    risk_reward = (
        f'单笔仓位约 {per_trade:.2f}，总敞口上限约 {max_total:.2f}；'
        f'总止损 {sl_total:.2f}，单笔止损 {sl_trade*100:.2f}% ，单笔止盈 {tp_trade*100:.2f}%。'
    )

    advice = '先在模拟盘观察成交质量、滑点与回撤，确认稳定后再小资金实盘。'
    if notes:
        advice = f"{'；'.join(notes)}。{advice}"

    return (
        '1. 这个策略的核心逻辑是什么\n'
        f'{core_logic}\n\n'
        '2. 在什么市场条件下会触发交易\n'
        f'{trigger_text}\n\n'
        '3. 预期的风险和收益特征\n'
        f'{risk_reward}\n\n'
        '4. 我的建议和注意事项\n'
        f'{advice}\n\n'
        '```strategy_json\n'
        f"{json.dumps(draft, ensure_ascii=False, indent=2)}\n"
        '```'
    )


def _workshop_apply_local_adjustment(user_text: str, draft: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    out = _workshop_force_executable_draft(draft, user_text=user_text)
    t = str(user_text or '').lower()
    notes: list[str] = []
    sizing = out.get('position_sizing', {}) if isinstance(out.get('position_sizing'), dict) else {}
    risk = out.get('risk_management', {}) if isinstance(out.get('risk_management'), dict) else {}

    if any(x in t for x in ['保守', '降低风险', '稳健', 'conservative']):
        per_trade = max(0.5, _safe_float(sizing.get('per_trade_usdc', 10.0), 10.0) * 0.7)
        sizing['per_trade_usdc'] = round(per_trade, 6)
        risk['stop_loss_per_trade_pct'] = round(min(-0.02, _safe_float(risk.get('stop_loss_per_trade_pct'), -0.08) * 0.8), 6)
        notes.append('已降低单笔仓位并收紧止损')
    if any(x in t for x in ['激进', '高频', 'aggressive']):
        per_trade = _safe_float(sizing.get('per_trade_usdc', 10.0), 10.0) * 1.3
        sizing['per_trade_usdc'] = round(per_trade, 6)
        out['check_interval_minutes'] = max(1, int(out.get('check_interval_minutes', 10)) // 2)
        notes.append('已提高进攻性并缩短检查间隔')
    if any(x in t for x in ['trump', '大选', 'election']):
        filt = out.get('market_filter', {}) if isinstance(out.get('market_filter'), dict) else {}
        filt['keywords'] = ['Trump', 'election']
        out['market_filter'] = filt
        notes.append('目标市场已收敛到 Trump/election 关键词')

    out['position_sizing'] = sizing
    out['risk_management'] = risk
    out = _workshop_force_executable_draft(out, user_text=user_text)
    reply = _workshop_render_reply(out, notes=notes)
    return reply, out


def _workshop_call_provider(
    provider: ModelProvider,
    messages: list[dict[str, str]],
    draft: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    adapter = str(provider.adapter or '').strip().lower()
    if adapter != 'openai_compatible':
        raise ValueError(f'provider={provider.provider_id} adapter={adapter} 暂不支持对话接口')

    endpoint = normalize_provider_endpoint(provider.endpoint, adapter='openai_compatible', company=provider.company)
    if not endpoint:
        raise ValueError(f'provider={provider.provider_id} endpoint 为空')

    api_messages: list[dict[str, str]] = [
        {
            'role': 'system',
            'content': (
                '你是 Polymarket 量化策略设计师。用户用自然语言描述交易想法，'
                '你需要将其转化为可执行的策略配置。'
                '你必须在回复末尾输出 JSON（用 ```strategy_json 包裹），包含：'
                '{'
                '"name":"策略中文名",'
                '"description":"一句话描述",'
                '"type":"ai_probability | arbitrage | market_making | spread_capture",'
                '"direction":"buy_yes | buy_no | both | market_make",'
                '"trigger_conditions":[{'
                '"type":"spread_threshold | ai_deviation | arb_gap | volume_filter | price_range",'
                '"operator":">= | <= | > | < | ==",'
                '"value":0.1,'
                '"description":"人类可读描述"'
                '}],'
                '"position_sizing":{"per_trade_usdc":20,"max_total_usdc":200},'
                '"risk_management":{"stop_loss_total":-50,"stop_loss_per_trade_pct":-0.1,'
                '"take_profit_per_trade_pct":0.15,"max_consecutive_losses":5},'
                '"market_filter":{"min_volume_24h":1000,"min_liquidity":500,"keywords":["Trump"] 或 "all"},'
                '"check_interval_minutes":15'
                '}'
                '必须给出可执行阈值，不要使用占位符条件。Polymarket 是 Yes/No 二元市场，'
                '价格范围 0-1，taker 手续费约 2%，阈值要覆盖手续费。'
            ),
        }
    ]
    for row in messages[-10:]:
        role = str(row.get('role', '')).strip().lower()
        content = str(row.get('content', '')).strip()
        if role not in {'user', 'assistant'} or not content:
            continue
        api_messages.append({'role': role, 'content': content[:4000]})
    api_messages.append(
        {
            'role': 'user',
            'content': (
                '当前策略草案 JSON：\n'
                f"{json.dumps(draft, ensure_ascii=False)}\n"
                '请结合上下文更新策略并按规则输出。'
            ),
        }
    )

    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }
    key = str(provider.api_key or '').strip()
    if key:
        headers['Authorization'] = f'Bearer {key}'
    for hk, hv in normalize_extra_headers(provider.extra_headers).items():
        headers[hk] = hv

    req_payload = {
        'model': str(provider.model or 'gpt-4o-mini'),
        'temperature': 0.2,
        'messages': api_messages,
    }
    req = Request(
        endpoint,
        data=json.dumps(req_payload, ensure_ascii=False).encode('utf-8'),
        headers=headers,
        method='POST',
    )
    with urlopen(req, timeout=min(40.0, max(10.0, settings.openclaw_timeout_sec))) as resp:
        body = json.loads(resp.read().decode('utf-8'))

    content = ''
    if isinstance(body, dict):
        choices = body.get('choices', [])
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            msg = choices[0].get('message', {})
            if isinstance(msg, dict):
                raw_content = msg.get('content', '')
                if isinstance(raw_content, list):
                    parts: list[str] = []
                    for part in raw_content:
                        if isinstance(part, dict):
                            txt = str(part.get('text', '') or part.get('content', '')).strip()
                            if txt:
                                parts.append(txt)
                    content = '\n'.join(parts)
                else:
                    content = str(raw_content or '').strip()
    reply = str(content or '').strip()
    if '```strategy_json' not in reply:
        raise ValueError('模型返回中缺少 strategy_json 代码块')
    parsed = _workshop_extract_strategy_json_block(reply)
    if not parsed:
        raise ValueError('模型返回中缺少 strategy_json 代码块或 JSON 不可解析')

    strategy_obj = parsed.get('strategy') if isinstance(parsed.get('strategy'), dict) else parsed
    strategy = _workshop_force_executable_draft(strategy_obj, user_text=messages[-1]['content'] if messages else '')
    return reply, strategy


def _workshop_next_strategy_id(existing_ids: list[str]) -> str:
    day = datetime.now().strftime('%Y%m%d')
    pat = re.compile(rf'^strat-{day}-(\d{{3}})$')
    max_seq = 0
    for sid in existing_ids:
        s = str(sid or '').strip()
        m = pat.match(s)
        if not m:
            continue
        try:
            max_seq = max(max_seq, int(m.group(1)))
        except Exception:
            continue
    return f'strat-{day}-{max_seq + 1:03d}'


def _workshop_trigger_value(draft: dict[str, Any], param_key: str, default: float) -> float:
    rows = draft.get('trigger_conditions')
    if not isinstance(rows, list):
        return default
    for row in rows:
        if not isinstance(row, dict):
            continue
        if 'type' in row and str(row.get('type', '')).strip().lower() == str(param_key or '').strip().lower():
            return _safe_float(row.get('value'), default)
        if str(row.get('param_key', '')).strip().lower() == str(param_key or '').strip().lower():
            return _safe_float(row.get('default'), default)
    return default


def _workshop_map_to_runtime(draft: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    stype = _workshop_normalize_type(draft.get('type', 'spread_capture'))
    direction = _workshop_normalize_direction(draft.get('direction', 'both'))
    sizing = draft.get('position_sizing', {}) if isinstance(draft.get('position_sizing'), dict) else {}
    risk = draft.get('risk_management', {}) if isinstance(draft.get('risk_management'), dict) else {}
    market_filter = draft.get('market_filter', {}) if isinstance(draft.get('market_filter'), dict) else {}
    check_minutes = max(1, int(_safe_float(draft.get('check_interval_minutes'), 15)))
    hold_ticks = max(1, min(60, int(round(check_minutes / 5.0))))
    order_notional = max(0.1, _safe_float(sizing.get('per_trade_usdc', sizing.get('per_trade')), 10.0))
    max_total = max(order_notional, _safe_float(sizing.get('max_total_usdc', sizing.get('max_total')), order_notional * 8.0))
    stop_trade_pct = abs(_safe_float(risk.get('stop_loss_per_trade_pct'), -0.1))
    if stop_trade_pct <= 1e-6:
        stop_trade_pct = 0.1
    max_consecutive_losses = max(1, int(round(_safe_float(risk.get('max_consecutive_losses'), 5))))
    keywords = _workshop_parse_keywords(market_filter.get('keywords', 'all'), default='all')

    runtime_type = 'workshop'
    params: dict[str, Any] = {
        'order_qty': round(order_notional, 6),
        'order_notional_usdc': round(order_notional, 6),
        'allow_min_size_override': True,
        'hold_ticks': hold_ticks,
        'risk_loss_limit_pct': round(max(0.2, min(50.0, stop_trade_pct * 100.0)), 6),
        'max_total_notional': round(max_total, 6),
        'max_consecutive_losses': max_consecutive_losses,
        'check_interval_minutes': check_minutes,
        'target_markets': ','.join(keywords) if isinstance(keywords, list) else str(keywords),
        'workshop_type': stype,
        'workshop_direction': direction,
        'workshop_trigger_all': True,
        'workshop_spec': draft,
        'workshop_mapped_at_utc': _now_utc_iso(),
        'workshop_market_filter': {
            'min_volume_24h': round(max(0.0, _safe_float(market_filter.get('min_volume_24h'), 0.0)), 6),
            'min_liquidity': round(max(0.0, _safe_float(market_filter.get('min_liquidity'), 0.0)), 6),
            'keywords': keywords,
        },
    }

    if stype == 'arbitrage':
        gap = abs(_workshop_trigger_value(draft, 'arb_gap', 0.04))
        params['arb_gap_threshold'] = round(max(0.0, gap), 6)
        params['arb_buy_threshold'] = round(1.0 - max(0.0, gap), 6)
        params['arb_sell_threshold'] = round(1.0 + max(0.0, gap), 6)
    elif stype == 'ai_probability':
        dev = abs(_workshop_trigger_value(draft, 'ai_deviation', 0.10))
        params['ai_prob_diff_threshold'] = round(max(0.0, dev), 6)
    elif stype == 'market_making':
        params['mm_min_spread'] = round(max(0.0, _workshop_trigger_value(draft, 'spread_threshold', 0.05)), 6)
        params['mm_min_volume'] = round(max(0.0, _workshop_trigger_value(draft, 'volume_filter', 1000.0)), 6)
    elif stype in {'spread_capture', 'custom'}:
        price_target = _workshop_trigger_value(draft, 'price_range', 0.5)
        params['price_target'] = round(max(0.0, min(1.0, price_target)), 6)

    return runtime_type, params


def _migrate_workshop_strategies_once() -> dict[str, int]:
    global workshop_migration_done
    with workshop_migration_lock:
        if workshop_migration_done:
            return {'checked': 0, 'migrated': 0}

        rows = strategy_store.load_strategies()
        if not rows:
            workshop_migration_done = True
            return {'checked': 0, 'migrated': 0}

        migrated = 0
        checked = 0
        out_rows: list[StrategyConfig] = []
        for row in rows:
            params = dict(row.params or {})
            source = str(row.source or '').strip().lower()
            has_spec = isinstance(params.get('workshop_spec'), dict)
            if source != 'workshop' and not has_spec:
                out_rows.append(row)
                continue

            checked += 1
            spec = params.get('workshop_spec') if isinstance(params.get('workshop_spec'), dict) else {}
            hint = ' '.join(
                [
                    str(row.name or ''),
                    str(spec.get('description', '')) if isinstance(spec, dict) else '',
                ]
            ).strip()
            draft = _workshop_force_executable_draft(spec if isinstance(spec, dict) else {}, user_text=hint)
            runtime_type, runtime_params = _workshop_map_to_runtime(draft)

            # keep user-adjusted runtime params while replacing stale placeholder workshop spec
            keep_keys = {
                'order_qty',
                'order_notional_usdc',
                'allow_min_size_override',
                'hold_ticks',
                'risk_loss_limit_pct',
                'max_total_notional',
                'max_consecutive_losses',
                'check_interval_minutes',
                'target_markets',
                'mean_rev_window',
                'mean_rev_threshold',
                'mm_min_spread',
                'mm_min_volume',
                'arb_buy_threshold',
                'arb_sell_threshold',
                'arb_gap_threshold',
                'ai_prob_diff_threshold',
                'ai_confidence_threshold',
                'workshop_direction',
                'workshop_trigger_all',
                'workshop_market_filter',
                'price_target',
            }
            merged_params = dict(runtime_params)
            for k, v in params.items():
                if k in keep_keys:
                    merged_params[k] = v
            merged_params['workshop_spec'] = draft
            merged_params['workshop_type'] = str(draft.get('type', 'custom'))
            merged_params['workshop_mapped_at_utc'] = _now_utc_iso()

            migrated_row = StrategyConfig(
                strategy_id=row.strategy_id,
                name=row.name or str(draft.get('name', row.strategy_id)),
                strategy_type=runtime_type,
                params=merged_params,
                enabled=bool(row.enabled),
                source=row.source,
                created_at_utc=row.created_at_utc,
            )
            if (
                migrated_row.strategy_type != row.strategy_type
                or json.dumps(migrated_row.params, ensure_ascii=False, sort_keys=True)
                != json.dumps(row.params or {}, ensure_ascii=False, sort_keys=True)
            ):
                migrated += 1
                quant_db.upsert_strategy(
                    {
                        'id': migrated_row.strategy_id,
                        'name': migrated_row.name,
                        'config_json': migrated_row.params,
                        'status': 'running' if migrated_row.enabled else 'paused',
                        'created_at': migrated_row.created_at_utc,
                        'stop_reason': '' if migrated_row.enabled else '手动暂停',
                    }
                )
                strategy_store.append_log(
                    {
                        'kind': 'workshop_strategy_migrated',
                        'strategy_id': migrated_row.strategy_id,
                        'from_type': row.strategy_type,
                        'to_type': migrated_row.strategy_type,
                    }
                )
            out_rows.append(migrated_row)

        if migrated > 0:
            strategy_store.save_strategies(out_rows)
        workshop_migration_done = True
        return {'checked': checked, 'migrated': migrated}


def _workshop_write_signal_template(
    strategy_id: str,
    draft: dict[str, Any],
    runtime_type: str,
    runtime_params: dict[str, Any],
) -> Path:
    folder = settings.paper_dir / 'live' / 'workshop_signals'
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f'{strategy_id}.py'
    spec_blob = repr(json.dumps(draft, ensure_ascii=False, sort_keys=True))
    params_blob = repr(json.dumps(runtime_params, ensure_ascii=False, sort_keys=True))
    code = (
        '"""Auto-generated signal template for workshop strategy."""\n'
        'from __future__ import annotations\n\n'
        'import json\n'
        'from typing import Any\n\n'
        f'STRATEGY_ID = {repr(strategy_id)}\n'
        f'RUNTIME_STRATEGY_TYPE = {repr(runtime_type)}\n'
        f'WORKSHOP_SPEC = json.loads({spec_blob})\n'
        f'RUNTIME_PARAMS = json.loads({params_blob})\n\n'
        'def generate_signal(context: dict[str, Any]) -> dict[str, Any]:\n'
        '    """Return a signal payload: action=buy/sell/hold."""\n'
        '    _ = context\n'
        '    return {\n'
        '        "strategy_id": STRATEGY_ID,\n'
        '        "action": "hold",\n'
        '        "reason": "template_generated",\n'
        '    }\n'
    )
    out.write_text(code, encoding='utf-8')
    return out


def _parse_iso_utc(value: Any) -> datetime | None:
    s = str(value or '').strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _llm_translation_provider_candidates() -> list[ModelProvider]:
    cfg = model_router_store.load()
    providers = [
        p
        for p in (cfg.providers or [])
        if bool(p.enabled)
        and str(p.endpoint or '').strip()
        and str(p.adapter or '').strip().lower() == 'openai_compatible'
    ]
    if not providers:
        return []

    prefer_id = str(_get_llm_health_state().get('provider_id', '')).strip()
    prefer: list[ModelProvider] = []
    rest: list[ModelProvider] = []
    for p in providers:
        if prefer_id and p.provider_id == prefer_id:
            prefer.append(p)
        else:
            rest.append(p)
    rest.sort(key=lambda x: (int(x.priority), str(x.provider_id)))
    seen: set[str] = set()
    out: list[ModelProvider] = []
    for p in prefer + rest:
        pid = str(p.provider_id or '').strip()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        out.append(p)
    return out


def _llm_openai_chat_content(
    *,
    provider: ModelProvider,
    system_text: str,
    user_text: str,
    timeout_sec: float = 20.0,
) -> str:
    endpoint = normalize_provider_endpoint(
        str(provider.endpoint or ''),
        adapter=str(provider.adapter or ''),
        company=str(provider.company or ''),
    )
    if not endpoint:
        raise ValueError('provider endpoint 为空')
    payload = {
        'model': str(provider.model or 'auto'),
        'temperature': 0.2,
        'max_tokens': 96,
        'messages': [
            {'role': 'system', 'content': system_text},
            {'role': 'user', 'content': user_text},
        ],
    }
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
    key = str(provider.api_key or '').strip()
    if key:
        headers['Authorization'] = f'Bearer {key}'
    headers.update(normalize_extra_headers(provider.extra_headers or {}))
    req = Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers=headers,
        method='POST',
    )
    with urlopen(req, timeout=max(6.0, timeout_sec)) as resp:
        body = json.loads(resp.read().decode('utf-8'))

    choices = body.get('choices', []) if isinstance(body, dict) else []
    content = ''
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        msg = choices[0].get('message', {})
        if isinstance(msg, dict):
            raw = msg.get('content', '')
            if isinstance(raw, list):
                parts: list[str] = []
                for it in raw:
                    if isinstance(it, dict):
                        txt = str(it.get('text', '') or it.get('content', '')).strip()
                        if txt:
                            parts.append(txt)
                content = '\n'.join(parts).strip()
            else:
                content = str(raw or '').strip()
    if not content:
        raise RuntimeError('LLM返回空内容')
    return content


def _cleanup_translated_name(name_zh: str, name_en: str) -> str:
    text = str(name_zh or '').strip()
    if not text:
        return ''
    if text.startswith('```'):
        text = text.replace('```', '').replace('json', '').strip()
    text = text.splitlines()[0].strip()
    text = text.strip('"').strip("'").strip()
    if not text:
        return ''
    if str(name_en or '').strip().endswith('?') and not text.endswith(('？', '?')):
        text = f'{text}？'
    if len(text) > 120:
        text = text[:120].rstrip()
    return text


def _translate_market_name_by_llm(name_en: str) -> str:
    src = str(name_en or '').strip()
    if not src:
        return ''
    system_text = '你是金融交易产品的翻译助手。只输出中文翻译结果，不要解释，不要加引号。'
    user_text = (
        f'将以下 Polymarket 市场名称翻译为简洁的中文，保持疑问句格式：{src}\n'
        '示例：\n'
        '- "Will Harvey Weinstein be sentenced to no prison time?" -> "韦恩斯坦会免于监禁吗？"\n'
        '- "Trump out as President before GTA VI?" -> "GTA6发售前特朗普会下台吗？"\n'
        '- "Russia-Ukraine Ceasefire before GTA VI?" -> "GTA6发售前俄乌会停火吗？"'
    )
    errors: list[str] = []
    for provider in _llm_translation_provider_candidates():
        try:
            content = _llm_openai_chat_content(
                provider=provider,
                system_text=system_text,
                user_text=user_text,
                timeout_sec=14.0,
            )
            cleaned = _cleanup_translated_name(content, src)
            if cleaned:
                return cleaned
        except Exception as exc:
            errors.append(f'{provider.provider_id}: {exc}')
            continue
    if errors:
        strategy_store.append_log({'kind': 'market_translate_error', 'name_en': src[:200], 'error': ' | '.join(errors[:3])})
    return ''


def _market_translation_worker() -> None:
    while True:
        market_id = ''
        name_en = ''
        try:
            market_id, name_en = market_translate_queue.get()
            mid = str(market_id or '').strip()
            en = str(name_en or '').strip()
            if not mid or not en:
                continue
            exists = quant_db.get_market_translation(mid)
            if isinstance(exists, dict) and str(exists.get('name_zh', '')).strip():
                continue
            name_zh = _translate_market_name_by_llm(en)
            if not name_zh:
                name_zh = en
            quant_db.upsert_market_translation(
                market_id=mid,
                name_en=en,
                name_zh=name_zh,
                translated_at=_now_utc_iso(),
            )
        except Exception as exc:
            if market_id and name_en:
                try:
                    quant_db.upsert_market_translation(
                        market_id=str(market_id),
                        name_en=str(name_en),
                        name_zh=str(name_en),
                        translated_at=_now_utc_iso(),
                    )
                except Exception:
                    pass
            strategy_store.append_log({'kind': 'market_translate_worker_error', 'error': str(exc)})
        finally:
            if market_id:
                with market_translate_lock:
                    market_translate_pending.discard(str(market_id))
            try:
                market_translate_queue.task_done()
            except Exception:
                pass


def _enqueue_market_translation(market_id: str, name_en: str) -> None:
    mid = str(market_id or '').strip()
    en = str(name_en or '').strip()
    if not mid or not en:
        return
    row = quant_db.get_market_translation(mid)
    if isinstance(row, dict) and str(row.get('name_zh', '')).strip():
        return
    with market_translate_lock:
        if mid in market_translate_pending:
            return
        market_translate_pending.add(mid)
    try:
        market_translate_queue.put_nowait((mid, en))
    except Exception:
        with market_translate_lock:
            market_translate_pending.discard(mid)


def _resolve_market_name_zh_en(market_id: str, name_en: str, trans_map: dict[str, dict[str, Any]] | None = None) -> tuple[str, str]:
    mid = str(market_id or '').strip()
    en = str(name_en or '').strip()
    row: dict[str, Any] | None = None
    if isinstance(trans_map, dict) and mid in trans_map and isinstance(trans_map.get(mid), dict):
        row = trans_map[mid]
    else:
        row = quant_db.get_market_translation(mid)
    if isinstance(row, dict):
        zh = str(row.get('name_zh', '')).strip()
        en_db = str(row.get('name_en', '')).strip()
        if zh:
            return zh, (en_db or en or zh)
    if mid and en:
        _enqueue_market_translation(mid, en)
    return en or mid, en or mid


def _quant_token_market_map() -> dict[str, str]:
    out: dict[str, str] = {}
    rows = quant_db.list_markets(limit=2000)
    market_ids: list[str] = []
    name_map: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        market_id = str(row.get('market_id', '')).strip()
        question = str(row.get('question', '')).strip()
        if not market_id or not question:
            continue
        market_ids.append(market_id)
        name_map[market_id] = question
    trans_map = quant_db.get_market_translations(market_ids)
    for row in rows:
        if not isinstance(row, dict):
            continue
        market_id = str(row.get('market_id', '')).strip()
        question = str(row.get('question', '')).strip()
        if not question:
            continue
        name_zh, _ = _resolve_market_name_zh_en(market_id, question, trans_map=trans_map)
        yes = str(row.get('yes_token_id', '')).strip()
        no = str(row.get('no_token_id', '')).strip()
        if yes:
            out[yes] = name_zh
        if no:
            out[no] = name_zh
    return out


def _quant_token_market_map_en() -> dict[str, str]:
    out: dict[str, str] = {}
    rows = quant_db.list_markets(limit=2000)
    for row in rows:
        if not isinstance(row, dict):
            continue
        question = str(row.get('question', '')).strip()
        if not question:
            continue
        yes = str(row.get('yes_token_id', '')).strip()
        no = str(row.get('no_token_id', '')).strip()
        if yes:
            out[yes] = question
        if no:
            out[no] = question
    return out


def _signal_reason_text(reason: Any, signal_type: str = '') -> str:
    if not isinstance(reason, dict):
        return ''
    decision = str(reason.get('decision_text', '')).strip()
    if decision:
        return decision
    rule = str(reason.get('rule', '')).strip().lower()
    if rule == 'moderate_liquidity_quote':
        spread = _safe_float(reason.get('spread', 0.0))
        min_spread_raw = reason.get('min_spread')
        if min_spread_raw is None:
            min_spread = _safe_float(quant_signal_engine.mm_min_spread, 0.0)
        else:
            min_spread = _safe_float(min_spread_raw, 0.0)
        min_spread = max(0.0, min_spread)
        spread_ok = spread >= min_spread - 1e-12 if min_spread > 0 else True
        op = '>=' if spread_ok else '<'
        return (
            f"spread={spread*100:.2f}% {op} min_spread={min_spread*100:.2f}%"
        )
    if rule == 'yes_ask + no_ask < threshold_with_fee':
        pair_cost = _safe_float(reason.get('pair_cost', 0.0))
        trigger = _safe_float(reason.get('trigger', 0.0))
        return f"Yes+No买入和={pair_cost:.4f} < 阈值={trigger:.4f}"
    if rule == 'yes_bid + no_bid > threshold_with_fee':
        pair_sum = _safe_float(reason.get('pair_bid_sum', 0.0))
        trigger = _safe_float(reason.get('trigger', 0.0))
        return f"Yes+No卖出和={pair_sum:.4f} > 阈值={trigger:.4f}"
    if rule == 'ai_probability_gap':
        dev = _safe_float(reason.get('deviation', 0.0))
        conf = _safe_float(reason.get('confidence', 0.0))
        return (
            f"AI偏差={dev*100:.2f}% >= 阈值={quant_signal_engine.ai_deviation_threshold*100:.2f}% "
            f"| conf={conf:.2f}"
        )
    if signal_type:
        return signal_type
    return ''


def _recent_signals_by_strategy(limit: int = 2500) -> dict[str, list[dict[str, Any]]]:
    rows = quant_db.list_signals(limit=max(1, min(limit, 5000)))
    out: dict[str, list[dict[str, Any]]] = {}

    def _append_recent(sid: str, item: dict[str, Any]) -> None:
        items = out.setdefault(sid, [])
        exists = False
        for old in items:
            if not isinstance(old, dict):
                continue
            if (
                str(old.get('time_utc', '')) == str(item.get('time_utc', ''))
                and str(old.get('signal_type', '')) == str(item.get('signal_type', ''))
                and str(old.get('side', '')) == str(item.get('side', ''))
            ):
                exists = True
                break
        if not exists:
            items.append(item)
        items.sort(key=lambda x: str(x.get('time_utc', '')), reverse=True)
        if len(items) > 3:
            del items[3:]

    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = str(row.get('strategy_id', '')).strip()
        if not sid:
            continue
        try:
            reason_obj = json.loads(str(row.get('reason_json', '{}')))
        except Exception:
            reason_obj = {}
        item = {
            'time_utc': str(row.get('time_utc', '')),
            'side': str(row.get('side', '')).upper(),
            'signal_type': str(row.get('signal_type', '')),
            'status': str(row.get('status', '')),
            'reason': _signal_reason_text(reason_obj, signal_type=str(row.get('signal_type', ''))),
        }
        _append_recent(sid, item)

    # Supplement with paper-bot strategy logs so workshop/template strategies also show recent signals.
    raw_logs = strategy_store.read_logs(limit=max(1000, min(limit * 8, 12000)))
    for row in reversed(raw_logs):
        if not isinstance(row, dict):
            continue
        kind = str(row.get('kind', '')).strip()
        if kind not in {'paper_bot_order', 'paper_bot_order_error', 'paper_bot_skip', 'paper_risk_halt', 'paper_bot_check'}:
            continue
        sid = str(row.get('strategy_id', '')).strip()
        if not sid:
            continue
        side = str(row.get('signal', row.get('decision', ''))).strip().lower()
        if kind == 'paper_bot_order':
            side = side if side in {'buy', 'sell'} else 'hold'
        elif kind == 'paper_bot_check':
            if side in {'buy_yes', 'buy_no', 'market_make'}:
                side = 'buy'
            side = side if side in {'buy', 'sell'} else 'hold'
        else:
            side = 'hold'
        reason = str(row.get('reason', '')).strip() or str(row.get('error', '')).strip() or str(row.get('message', '')).strip()
        status = 'executed' if kind == 'paper_bot_order' else ('failed' if kind == 'paper_bot_order_error' else ('checked' if kind == 'paper_bot_check' else 'skipped'))
        item = {
            'time_utc': str(row.get('time_utc', '')),
            'side': side.upper(),
            'signal_type': kind,
            'status': status,
            'reason': reason or kind,
        }
        _append_recent(sid, item)

    # Supplement quant scan summaries so "无机会"也能可视化，而不是误解为策略停摆。
    evt_rows = quant_db.list_events(limit=max(200, min(limit * 2, 2000)))
    for row in evt_rows:
        if not isinstance(row, dict):
            continue
        kind = str(row.get('kind', '')).strip()
        if kind not in {'arb_scan_summary', 'mm_scan', 'ai_scan_summary'}:
            continue
        sid = ''
        side = 'SCAN'
        status = 'checked'
        reason = ''
        try:
            payload = json.loads(str(row.get('payload_json', '{}')))
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        if kind == 'arb_scan_summary':
            sid = 'arb_detector'
            markets = int(payload.get('markets', 0))
            min_buy_sum = _safe_float(payload.get('min_buy_sum', 0.0))
            max_sell_sum = _safe_float(payload.get('max_sell_sum', 0.0))
            buy_threshold = _safe_float(payload.get('buy_threshold', quant_signal_engine.arb_buy_threshold))
            sell_threshold = _safe_float(payload.get('sell_threshold', quant_signal_engine.arb_sell_threshold))
            if markets <= 0:
                reason = '未扫描到可用市场'
            elif min_buy_sum >= buy_threshold and max_sell_sum <= sell_threshold:
                reason = (
                    f"已扫描 {markets} 市场，最小Yes+No={min_buy_sum:.4f}，最大Yes+No={max_sell_sum:.4f}，无套利机会"
                )
            else:
                reason = (
                    f"已扫描 {markets} 市场，最小Yes+No={min_buy_sum:.4f}，最大Yes+No={max_sell_sum:.4f}，存在候选机会"
                )
        elif kind == 'mm_scan':
            sid = 'market_maker'
            scanned = int(payload.get('scanned_markets', 0))
            strict = int(payload.get('strict_candidates', 0))
            selected = int(payload.get('selected_markets', 0))
            reason = f"已扫描 {scanned} 市场，符合做市条件 {strict}，本轮选中 {selected}"
        elif kind == 'ai_scan_summary':
            sid = 'ai_probability'
            active = int(payload.get('active_markets', 0))
            target = int(payload.get('target_markets', 0))
            evaluated = int(payload.get('evaluated_markets', 0))
            triggered = int(payload.get('triggered_signals', 0))
            reason = f"活跃 {active} 市场，目标 {target}，本轮评估 {evaluated}，触发 {triggered}"

        if not sid:
            continue
        item = {
            'time_utc': str(row.get('time_utc', '')),
            'side': side,
            'signal_type': kind,
            'status': status,
            'reason': reason or str(row.get('message', '')).strip() or kind,
        }
        _append_recent(sid, item)
    return out


def _hours_since(dt: datetime | None, now: datetime) -> float:
    if dt is None:
        return 0.0
    return max(0.0, (now - dt).total_seconds() / 3600.0)


def _build_strategy_pnl_series(strategy_id: str, *, limit: int = 5000) -> dict[str, Any]:
    sid = str(strategy_id or '').strip()
    if not sid:
        return {
            'metrics': {
                'total_pnl': 0.0,
                'today_pnl': 0.0,
                'win_rate': 0.0,
                'profit_factor': 0.0,
                'max_drawdown': 0.0,
                'trade_count': 0,
            },
            'rows': [],
        }

    now = datetime.now(timezone.utc)
    day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)

    q_rows = quant_db.fetch_all(
        """
        SELECT time_utc, strategy_id, side, price, size, fee, pnl_delta, token_id
        FROM q_fill
        WHERE strategy_id = ?
        ORDER BY id ASC
        LIMIT ?
        """,
        (sid, max(1, min(limit, 20000))),
    )
    use_quant = any(isinstance(x, dict) for x in q_rows)
    rows: list[dict[str, Any]] = q_rows if use_quant else paper_engine.list_fills(limit=max(1, min(limit, 20000)), strategy_id=sid)

    positions: dict[str, float] = {}
    avg_cost: dict[str, float] = {}
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    wins = 0
    losses = 0
    profit_sum = 0.0
    loss_sum = 0.0
    today_pnl = 0.0
    series: list[dict[str, Any]] = []

    for row in rows:
        if not isinstance(row, dict):
            continue
        token_id = str(row.get('token_id', '')).strip()
        side = str(row.get('side', '')).strip().lower()
        qty = _safe_float(row.get('size', row.get('quantity', 0.0)))
        price = _safe_float(row.get('price', 0.0))
        fee = _safe_float(row.get('fee', 0.0))
        time_utc = str(row.get('time_utc', ''))
        ts = _parse_iso_utc(time_utc)
        if qty <= 0 or price <= 0:
            continue

        pnl_delta = 0.0
        if use_quant:
            pnl_delta = _safe_float(row.get('pnl_delta', 0.0))
        else:
            pos = max(0.0, _safe_float(positions.get(token_id, 0.0)))
            avg = _safe_float(avg_cost.get(token_id, 0.0))
            if side == 'buy':
                new_pos = pos + qty
                notional = qty * price
                if new_pos > 1e-12:
                    avg_cost[token_id] = ((avg * pos) + notional) / new_pos
                positions[token_id] = new_pos
                pnl_delta = -fee
            elif side == 'sell':
                sell_qty = min(qty, pos)
                pnl_delta = (price - avg) * sell_qty - fee
                new_pos = max(0.0, pos - sell_qty)
                positions[token_id] = new_pos
                if new_pos <= 1e-12:
                    avg_cost[token_id] = 0.0
            else:
                continue

        cum += pnl_delta
        peak = max(peak, cum)
        drawdown = peak - cum
        max_dd = max(max_dd, drawdown)
        if pnl_delta > 1e-12:
            wins += 1
            profit_sum += pnl_delta
        elif pnl_delta < -1e-12:
            losses += 1
            loss_sum += abs(pnl_delta)

        if ts is not None and ts >= day_start:
            today_pnl += pnl_delta
        series.append(
            {
                'time_utc': time_utc,
                'value': round(cum, 10),
                'delta': round(pnl_delta, 10),
                'strategy_id': sid,
            }
        )

    trades = wins + losses
    win_rate = (wins / trades) if trades > 0 else 0.0
    if loss_sum <= 1e-12:
        profit_factor = profit_sum if profit_sum > 0 else 0.0
    else:
        profit_factor = profit_sum / loss_sum
    return {
        'metrics': {
            'total_pnl': round(cum, 10),
            'today_pnl': round(today_pnl, 10),
            'win_rate': round(win_rate, 10),
            'profit_factor': round(profit_factor, 10),
            'max_drawdown': round(max_dd, 10),
            'trade_count': int(trades),
        },
        'rows': series,
    }


def _build_strategy_ai_insights(strategy_id: str, *, limit: int = 30) -> list[dict[str, Any]]:
    sid = str(strategy_id or '').strip()
    if not sid:
        return []
    lim = max(1, min(limit, 200))
    signal_rows = _materialize_strategy_signals(sid, limit=max(60, lim * 5))
    trade_rows = _materialize_strategy_trades(sid, limit=max(120, lim * 8))
    trade_by_signal: dict[int, dict[str, Any]] = {}
    for row in trade_rows:
        if not isinstance(row, dict):
            continue
        sid_num = row.get('signal_id')
        try:
            key = int(sid_num)
        except Exception:
            continue
        if key not in trade_by_signal:
            trade_by_signal[key] = row

    market_rows = quant_db.list_markets(limit=2000)
    market_map = {str(x.get('market_id', '')): x for x in market_rows if isinstance(x, dict)}
    trans_map = quant_db.get_market_translations([str(x.get('market_id', '')).strip() for x in market_rows if isinstance(x, dict)])

    out: list[dict[str, Any]] = []
    for row in signal_rows:
        if len(out) >= lim:
            break
        if not isinstance(row, dict):
            continue
        source_signal_id = row.get('source_signal_id')
        signal_id = 0
        try:
            signal_id = int(source_signal_id)
        except Exception:
            signal_id = 0
        trade = trade_by_signal.get(signal_id, {})

        market_id = str(row.get('market_id', '')).strip()
        market = market_map.get(market_id, {})
        market_name_zh, market_name_en = _resolve_market_name_zh_en(
            market_id,
            str(market.get('question', '')).strip() or str(row.get('source_text', '')).strip() or market_id,
            trans_map=trans_map,
        )
        source_title = str(row.get('source_text', '')).strip() or str(market.get('question', '')).strip() or market_id
        source_name = ''
        source_url = str(row.get('source_url', '')).strip()
        market_name = market_name_zh or source_title
        decision_en = str(row.get('decision', 'hold')).strip().lower()
        decision = '不操作'
        if decision_en == 'buy':
            decision = '买入'
        elif decision_en == 'sell':
            decision = '卖出'
        triggered = decision_en in {'buy', 'sell'}
        signal_status = 'executed' if bool(trade) else ('pending' if triggered else 'hold')

        execution = ''
        if trade:
            action = '买入' if str(trade.get('side', '')).strip().lower() == 'buy' else '卖出'
            execution = (
                f"{action} @ {_safe_float(trade.get('price', 0.0)):.4f}, "
                f"数量 {_safe_float(trade.get('quantity', 0.0)):.4f} "
                f"(${_safe_float(trade.get('cost_usdc', 0.0)):.2f})"
            )
        out.append(
            {
                'time_utc': str(row.get('timestamp', '')),
                'market_id': market_id,
                'market_name': market_name,
                'market_name_en': market_name_en,
                'source_title': source_title,
                'source_url': source_url,
                'source_name': source_name,
                'ai_probability': _safe_float(row.get('ai_probability', 0.0)),
                'confidence': _safe_float(row.get('ai_confidence', 0.0)),
                'market_yes_price': _safe_float(row.get('market_price', 0.0)),
                'deviation': _safe_float(row.get('deviation', 0.0)),
                'decision': decision,
                'decision_reason': str(row.get('decision_reason', '')).strip(),
                'execution': execution,
                'triggered': triggered,
                'signal_status': signal_status,
                'model': '',
                'signal_type': str(row.get('signal_type', '')),
                'signal_id': signal_id,
            }
        )
    return out


def _strategy_rows_csv(rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            'strategy_id',
            'name',
            'status',
            'source',
            'strategy_type',
            'enabled',
            'total_pnl',
            'win_rate',
            'trade_count',
            'max_drawdown_pct',
            'equity',
            'open_orders',
            'runtime_hours',
            'pause_reason',
            'stop_reason',
            'created_at_utc',
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row.get('strategy_id', ''),
                row.get('name', ''),
                row.get('status', ''),
                row.get('source', ''),
                row.get('strategy_type', ''),
                1 if bool(row.get('enabled', False)) else 0,
                float(row.get('total_pnl', 0.0)),
                float(row.get('win_rate', 0.0)),
                int(row.get('trade_count', 0)),
                float(row.get('max_drawdown_pct', 0.0)),
                float(row.get('equity', 0.0)),
                int(row.get('open_orders', 0)),
                float(row.get('runtime_hours', 0.0)),
                str(row.get('pause_reason', '')),
                str(row.get('stop_reason', '')),
                str(row.get('created_at_utc', '')),
            ]
        )
    return buf.getvalue()


def _is_builtin_quant_strategy(strategy_id: str) -> bool:
    return str(strategy_id or '').strip() in {'arb_detector', 'market_maker', 'ai_probability'}


def _snapshot_strategy_config(cfg: StrategyConfig) -> dict[str, Any]:
    return {
        'strategy_id': cfg.strategy_id,
        'name': cfg.name,
        'strategy_type': cfg.strategy_type,
        'params': dict(cfg.params or {}),
        'enabled': bool(cfg.enabled),
        'source': cfg.source,
        'created_at_utc': cfg.created_at_utc,
    }


def _snapshot_builtin_strategy(strategy_id: str) -> dict[str, Any]:
    sid = str(strategy_id or '').strip()
    enabled = True
    with quant_orchestrator._lock:
        if sid == 'arb_detector':
            enabled = bool(quant_orchestrator._cfg.enable_arb)
        elif sid == 'market_maker':
            enabled = bool(quant_orchestrator._cfg.enable_mm)
        elif sid == 'ai_probability':
            enabled = bool(quant_orchestrator._cfg.enable_ai)
    return {
        'strategy_id': sid,
        'name': sid,
        'strategy_type': 'quant_builtin',
        'params': _quant_params_payload(),
        'enabled': enabled,
        'source': 'quant',
        'created_at_utc': _now_utc_iso(),
    }


def _record_strategy_version(
    strategy_id: str,
    snapshot: dict[str, Any],
    *,
    note: str = '',
    created_by: str = 'system',
    source: str = '',
) -> dict[str, Any]:
    sid = str(strategy_id or '').strip()
    if not sid:
        return {'id': 0, 'strategy_id': '', 'version_no': 0, 'created_at': ''}
    label = str(snapshot.get('name', sid)).strip() or sid
    return quant_db.insert_strategy_version(
        strategy_id=sid,
        config=snapshot,
        note=note,
        created_by=created_by,
        label=label,
        source=source,
        created_at=_now_utc_iso(),
    )


def _strategy_versions_payload(strategy_id: str, *, limit: int = 80) -> list[dict[str, Any]]:
    sid = str(strategy_id or '').strip()
    if not sid:
        return []
    rows = quant_db.list_strategy_versions(sid, limit=max(1, min(limit, 500)))
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cfg_obj: Any = {}
        try:
            cfg_obj = json.loads(str(row.get('config_json', '{}')))
        except Exception:
            cfg_obj = {}
        strategy_type = ''
        enabled = True
        param_count = 0
        params: dict[str, Any] = {}
        if isinstance(cfg_obj, dict):
            strategy_type = str(cfg_obj.get('strategy_type', ''))
            enabled = bool(cfg_obj.get('enabled', True))
            params = cfg_obj.get('params', {}) if isinstance(cfg_obj.get('params'), dict) else {}
            param_count = len(params)
        out.append(
            {
                'id': int(row.get('id', 0)),
                'strategy_id': sid,
                'version_no': int(row.get('version_no', 0)),
                'label': str(row.get('label', '')),
                'note': str(row.get('note', '')),
                'source': str(row.get('source', '')),
                'created_by': str(row.get('created_by', '')),
                'created_at': str(row.get('created_at', '')),
                'summary': {
                    'strategy_type': strategy_type,
                    'enabled': enabled,
                    'param_count': param_count,
                    'param_keys': sorted(list(params.keys()))[:10],
                },
            }
        )
    return out


def _ensure_paper_simulation_running(*, reason: str = 'workshop_deploy') -> dict[str, Any]:
    st = paper_bot_manager.status()
    if bool(getattr(st, 'running', False)):
        return {
            'auto_started': False,
            'running': True,
            'token_id': str(getattr(st, 'token_id', '')),
            'interval_sec': int(getattr(st, 'interval_sec', 12)),
            'source': 'existing',
        }

    market_payload = paper_markets(limit=40, active=True, closed=False)
    rows = market_payload.get('rows', []) if isinstance(market_payload, dict) else []
    token = PaperAutoQuantManager._pick_token(rows if isinstance(rows, list) else [])
    token_id = str(token.get('token_id', '')).strip()
    if not token_id:
        return {'auto_started': False, 'running': False, 'token_id': '', 'error': 'no_token'}

    if settings.paper_use_market_ws:
        try:
            paper_market_stream.start(assets_ids=[token_id])
        except Exception:
            pass
        try:
            paper_market_stream.add_assets([token_id])
        except Exception:
            pass
    paper_bot_manager.start(token_id=token_id, interval_sec=12, prefer_stream=True)
    strategy_store.append_log({'kind': 'workshop_auto_start_paper_bot', 'token_id': token_id, 'reason': reason})
    st2 = paper_bot_manager.status()
    return {
        'auto_started': True,
        'running': bool(getattr(st2, 'running', False)),
        'token_id': str(getattr(st2, 'token_id', token_id)),
        'interval_sec': int(getattr(st2, 'interval_sec', 12)),
        'source': 'auto_start',
    }


def _stable_int_from_text(text: str) -> int:
    raw = str(text or '').encode('utf-8')
    digest = hashlib.sha1(raw).digest()[:8]
    return int.from_bytes(digest, byteorder='big', signed=False) & 0x7FFFFFFFFFFFFFFF


def _sync_strategy_registry(runtime_rows: list[dict[str, Any]]) -> None:
    for row in runtime_rows:
        if not isinstance(row, dict):
            continue
        sid = str(row.get('strategy_id', '')).strip()
        if not sid:
            continue
        status = str(row.get('status', 'stopped')).strip().lower()
        stop_reason = ''
        if status == 'paused':
            stop_reason = str(row.get('pause_reason', '')).strip()
        elif status in {'stopped', 'archived'}:
            stop_reason = str(row.get('stop_reason', '')).strip()
        quant_db.upsert_strategy(
            {
                'id': sid,
                'name': str(row.get('name', sid)),
                'config_json': row.get('params', {}),
                'status': status,
                'created_at': str(row.get('created_at_utc', '')),
                'stopped_at': _now_utc_iso() if status in {'stopped', 'archived'} else '',
                'stop_reason': stop_reason,
            }
        )


def _materialize_strategy_signals(strategy_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
    sid = str(strategy_id or '').strip()
    if not sid:
        return []
    lim = max(1, min(limit, 5000))
    market_rows = quant_db.list_markets(limit=2000)
    market_map = {str(x.get('market_id', '')): x for x in market_rows if isinstance(x, dict)}
    token_market_map: dict[str, tuple[str, str]] = {}
    for row in market_rows:
        if not isinstance(row, dict):
            continue
        market_id = str(row.get('market_id', '')).strip()
        question = str(row.get('question', '')).strip()
        yes = str(row.get('yes_token_id', '')).strip()
        no = str(row.get('no_token_id', '')).strip()
        if yes:
            token_market_map[yes] = (market_id, question)
        if no:
            token_market_map[no] = (market_id, question)

    sig_rows = quant_db.fetch_all(
        """
        SELECT id, time_utc, strategy_id, signal_type, market_id, token_id, side, status, status_message, reason_json
        FROM q_signal
        WHERE strategy_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (sid, lim * 3),
    )
    ai_rows = quant_db.fetch_all(
        "SELECT * FROM q_ai_eval ORDER BY evaluated_at_utc DESC LIMIT ?",
        (2000,),
    )
    ai_map = {str(x.get('market_id', '')): x for x in ai_rows if isinstance(x, dict)}

    for row in sig_rows:
        if not isinstance(row, dict):
            continue
        market_id = str(row.get('market_id', '')).strip()
        token_id = str(row.get('token_id', '')).strip()
        market = market_map.get(market_id, {})
        ai = ai_map.get(market_id, {})
        reason_obj = {}
        try:
            reason_obj = json.loads(str(row.get('reason_json', '{}')))
        except Exception:
            reason_obj = {}

        signal_type = str(row.get('signal_type', '')).strip()
        source_text = str(market.get('question', '')).strip()
        source_url = ''
        ai_prob = 0.0
        ai_conf = 0.0
        market_price = _safe_float(market.get('yes_mid', 0.0))
        deviation = 0.0
        if signal_type.startswith('ai_'):
            source_text = str(ai.get('question', '')).strip() or source_text
            ai_prob = _safe_float(ai.get('probability', reason_obj.get('probability', 0.0)))
            ai_conf = _safe_float(ai.get('confidence', reason_obj.get('confidence', 0.0)))
            if market_price <= 0:
                market_price = _safe_float(reason_obj.get('market_price', reason_obj.get('yes_mid', 0.0)))
            if market_price > 0:
                deviation = ai_prob - market_price
            try:
                news = json.loads(str(ai.get('news_json', '[]')))
            except Exception:
                news = []
            if isinstance(news, list) and news and isinstance(news[0], dict):
                source_text = str(news[0].get('title', source_text))
                source_url = str(news[0].get('url', news[0].get('link', ''))).strip()
        else:
            if market_price <= 0:
                market_price = _safe_float(reason_obj.get('best_ask', reason_obj.get('best_bid', 0.0)))

        decision = 'hold'
        side = str(row.get('side', '')).strip().lower()
        status = str(row.get('status', '')).strip().lower()
        if side in {'buy', 'sell'} and status in {'executed', 'new', 'ready', 'submitted'}:
            decision = side
        if status in {'blocked', 'failed', 'cancelled'}:
            decision = 'hold'
        decision_reason = _signal_reason_text(reason_obj, signal_type=signal_type)
        if not decision_reason:
            decision_reason = str(row.get('status_message', '')).strip()

        quant_db.insert_strategy_signal(
            {
                'strategy_id': sid,
                'timestamp': str(row.get('time_utc', '')),
                'signal_type': signal_type or 'signal',
                'source_text': source_text,
                'source_url': source_url,
                'ai_probability': ai_prob,
                'ai_confidence': ai_conf,
                'market_price': market_price,
                'deviation': deviation,
                'decision': decision,
                'decision_reason': decision_reason,
                'market_id': market_id,
                'token_id': token_id,
                'source_signal_id': row.get('id'),
                'created_at': _now_utc_iso(),
            }
        )

    # Supplement for paper/workshop strategies that bypass q_signal.
    raw_logs = strategy_store.read_logs(limit=max(1000, min(lim * 30, 25000)))
    for row in raw_logs:
        if not isinstance(row, dict):
            continue
        if str(row.get('strategy_id', '')).strip() != sid:
            continue
        kind = str(row.get('kind', '')).strip()
        if kind not in {'paper_bot_order', 'paper_bot_order_error', 'paper_bot_skip', 'paper_risk_halt', 'paper_bot_check'}:
            continue
        token_id = str(row.get('token_id', '')).strip()
        market_id = str(row.get('market_id', '')).strip()
        market_question = str(row.get('market_name', '')).strip()
        if not market_id:
            market_id, market_question = token_market_map.get(token_id, ('', ''))
        market = market_map.get(market_id, {})
        source_text = str(market.get('question', '')).strip() or market_question or token_id

        side = str(row.get('signal', '')).strip().lower()
        if kind == 'paper_bot_check':
            side = str(row.get('decision', '')).strip().lower()
            if side in {'buy_yes', 'buy_no', 'market_make'}:
                side = 'buy'
            if side not in {'buy', 'sell'}:
                side = 'hold'
        elif kind != 'paper_bot_order' or side not in {'buy', 'sell'}:
            side = 'hold'
        signal_type = {
            'paper_bot_order': 'paper_bot_signal',
            'paper_bot_order_error': 'paper_bot_error',
            'paper_bot_skip': 'paper_bot_skip',
            'paper_risk_halt': 'paper_risk_halt',
            'paper_bot_check': 'paper_bot_check',
        }.get(kind, kind)

        decision_reason = (
            str(row.get('reason', '')).strip()
            or str(row.get('error', '')).strip()
            or str(row.get('message', '')).strip()
            or kind
        )
        market_price = _safe_float(row.get('price', 0.0))
        if market_price <= 0:
            market_price = _safe_float(row.get('market_price', 0.0))
        if market_price <= 0:
            market_price = _safe_float(market.get('yes_mid', 0.0))
        ai_probability = _safe_float(row.get('ai_probability', 0.0))
        ai_confidence = _safe_float(row.get('ai_confidence', 0.0))
        deviation = _safe_float(row.get('deviation', 0.0))
        if deviation <= 0 and ai_probability > 0 and market_price > 0:
            deviation = ai_probability - market_price

        fingerprint = (
            f"{sid}|{row.get('time_utc', '')}|{kind}|{row.get('tick', '')}|{market_id}|{token_id}|"
            f"{row.get('signal', '')}|{row.get('price', '')}|{row.get('size', '')}|{row.get('error', '')}"
        )
        source_signal_id = -max(1, _stable_int_from_text(fingerprint))
        quant_db.insert_strategy_signal(
            {
                'strategy_id': sid,
                'timestamp': str(row.get('time_utc', '')),
                'signal_type': signal_type,
                'source_text': source_text,
                'source_url': '',
                'ai_probability': ai_probability,
                'ai_confidence': ai_confidence,
                'market_price': market_price,
                'deviation': deviation,
                'decision': side,
                'decision_reason': decision_reason,
                'market_id': market_id,
                'token_id': token_id,
                'source_signal_id': source_signal_id,
                'created_at': _now_utc_iso(),
            }
        )
    return quant_db.list_strategy_signals(sid, limit=lim)


def _materialize_strategy_trades(strategy_id: str, *, limit: int = 400) -> list[dict[str, Any]]:
    sid = str(strategy_id or '').strip()
    if not sid:
        return []
    lim = max(1, min(limit, 5000))
    _materialize_strategy_signals(sid, limit=max(300, min(lim * 3, 5000)))
    market_map = _quant_token_market_map_en()
    strategy_row = quant_db.get_strategy(sid) or {}
    archived = str(strategy_row.get('status', '')).strip().lower() == 'archived'

    fill_rows = quant_db.fetch_all(
        """
        SELECT id, time_utc, strategy_id, signal_id, token_id, side, price, size, notional, pnl_delta
        FROM q_fill
        WHERE strategy_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (sid, lim * 3),
    )
    signal_ids: set[int] = set()
    for row in fill_rows:
        if not isinstance(row, dict):
            continue
        sid_val = row.get('signal_id')
        try:
            signal_ids.add(int(sid_val))
        except Exception:
            continue
    sig_map: dict[int, dict[str, Any]] = {}
    if signal_ids:
        placeholders = ','.join(['?'] * len(signal_ids))
        sig_rows = quant_db.fetch_all(
            f"SELECT id, signal_type, reason_json FROM q_signal WHERE id IN ({placeholders})",
            tuple(sorted(signal_ids)),
        )
        for row in sig_rows:
            if not isinstance(row, dict):
                continue
            try:
                rid = int(row.get('id', 0))
            except Exception:
                continue
            sig_map[rid] = row

    if fill_rows:
        for row in fill_rows:
            if not isinstance(row, dict):
                continue
            token_id = str(row.get('token_id', '')).strip()
            signal_id = row.get('signal_id')
            signal = {}
            try:
                if signal_id is not None:
                    signal = sig_map.get(int(signal_id), {}) or {}
            except Exception:
                signal = {}
            reason_obj = {}
            try:
                reason_obj = json.loads(str(signal.get('reason_json', '{}')))
            except Exception:
                reason_obj = {}
            decision_reason = _signal_reason_text(reason_obj, signal_type=str(signal.get('signal_type', '')))
            if not decision_reason:
                decision_reason = str(reason_obj.get('decision_text', '')).strip()
            fill_id = int(row.get('id', 0))
            quant_db.insert_strategy_trade(
                {
                    'strategy_id': sid,
                    'timestamp': str(row.get('time_utc', '')),
                    'side': str(row.get('side', '')).strip().lower(),
                    'market': market_map.get(token_id, token_id[:18]),
                    'price': _safe_float(row.get('price', 0.0)),
                    'quantity': _safe_float(row.get('size', 0.0)),
                    'cost_usdc': _safe_float(row.get('notional', 0.0)),
                    'pnl': _safe_float(row.get('pnl_delta', 0.0)),
                    'signal_id': signal_id,
                    'decision_reason': decision_reason,
                    'archived': archived,
                    'source_fill_id': fill_id,
                    'created_at': _now_utc_iso(),
                }
            )
    else:
        legacy_fills = paper_engine.list_fills(limit=lim * 3, strategy_id=sid)
        signal_rows = quant_db.list_strategy_signals(sid, limit=5000)
        signal_candidates: list[dict[str, Any]] = []
        for sig in signal_rows:
            if not isinstance(sig, dict):
                continue
            decision = str(sig.get('decision', '')).strip().lower()
            if decision not in {'buy', 'sell'}:
                continue
            ts = _parse_iso_utc(sig.get('timestamp'))
            if ts is None:
                continue
            signal_candidates.append(
                {
                    'time_utc': str(sig.get('timestamp', '')),
                    'dt': ts,
                    'token_id': str(sig.get('token_id', '')).strip(),
                    'decision': decision,
                    'source_signal_id': sig.get('source_signal_id'),
                    'decision_reason': str(sig.get('decision_reason', '')).strip(),
                }
            )
        for row in legacy_fills:
            if not isinstance(row, dict):
                continue
            token_id = str(row.get('token_id', '')).strip()
            fill_uid = str(row.get('fill_id', ''))
            if not fill_uid:
                fill_uid = f"{sid}:{row.get('time_utc', '')}:{token_id}:{row.get('side', '')}:{row.get('price', '')}:{row.get('quantity', '')}"
            source_fill_id = _stable_int_from_text(fill_uid)
            qty = _safe_float(row.get('quantity', row.get('size', 0.0)))
            px = _safe_float(row.get('price', 0.0))
            fill_dt = _parse_iso_utc(row.get('time_utc'))
            fill_side = str(row.get('side', '')).strip().lower()
            matched_signal_id: int | None = None
            matched_reason = ''
            if fill_dt is not None and fill_side in {'buy', 'sell'}:
                best_gap_sec: float | None = None
                for sig in signal_candidates:
                    if str(sig.get('decision', '')) != fill_side:
                        continue
                    sig_token = str(sig.get('token_id', '')).strip()
                    if sig_token and sig_token != token_id:
                        continue
                    sig_dt = sig.get('dt')
                    if not isinstance(sig_dt, datetime):
                        continue
                    gap_sec = abs((fill_dt - sig_dt).total_seconds())
                    if gap_sec > 600:
                        continue
                    if best_gap_sec is None or gap_sec < best_gap_sec:
                        best_gap_sec = gap_sec
                        src_sid = sig.get('source_signal_id')
                        try:
                            matched_signal_id = int(src_sid) if src_sid is not None else None
                        except Exception:
                            matched_signal_id = None
                        matched_reason = str(sig.get('decision_reason', '')).strip()
            quant_db.insert_strategy_trade(
                {
                    'strategy_id': sid,
                    'timestamp': str(row.get('time_utc', '')),
                    'side': str(row.get('side', '')).strip().lower(),
                    'market': market_map.get(token_id, token_id[:18]),
                    'price': px,
                    'quantity': qty,
                    'cost_usdc': qty * px,
                    'pnl': 0.0,
                    'signal_id': matched_signal_id,
                    'decision_reason': matched_reason or str(row.get('source', '')).strip(),
                    'archived': archived,
                    'source_fill_id': source_fill_id,
                    'created_at': _now_utc_iso(),
                }
            )

    return quant_db.list_strategy_trades(sid, limit=lim, include_archived=True)


def _format_strategy_trades(strategy_id: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sid = str(strategy_id or '').strip()
    if not sid:
        return []
    sig_rows = quant_db.list_strategy_signals(sid, limit=max(300, len(rows) * 8))
    sig_market_ids = [str(x.get('market_id', '')).strip() for x in sig_rows if isinstance(x, dict) and str(x.get('market_id', '')).strip()]
    trans_map = quant_db.get_market_translations(sig_market_ids)
    sig_map: dict[int, dict[str, Any]] = {}
    for row in sig_rows:
        if not isinstance(row, dict):
            continue
        source_signal_id = row.get('source_signal_id')
        if source_signal_id is None:
            continue
        try:
            key = int(source_signal_id)
        except Exception:
            continue
        if key not in sig_map:
            sig_map[key] = row

    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        signal_id = row.get('signal_id')
        sig = {}
        try:
            if signal_id is not None:
                sig = sig_map.get(int(signal_id), {}) or {}
        except Exception:
            sig = {}
        market_id = str(sig.get('market_id', '')).strip()
        market_en = str(row.get('market', '')).strip() or str(sig.get('source_text', '')).strip()
        market_zh, market_en = _resolve_market_name_zh_en(market_id, market_en, trans_map=trans_map)
        out.append(
            {
                'id': int(row.get('id', 0)),
                'time_utc': str(row.get('timestamp', '')),
                'side': str(row.get('side', '')).upper(),
                'market': market_zh,
                'market_en': market_en,
                'price': _safe_float(row.get('price', 0.0)),
                'quantity': _safe_float(row.get('quantity', 0.0)),
                'cost_usdc': _safe_float(row.get('cost_usdc', 0.0)),
                'pnl': _safe_float(row.get('pnl', 0.0)),
                'decision_reason': str(row.get('decision_reason', '')),
                'signal_id': signal_id,
                'market_id': market_id,
                'signal_type': str(sig.get('signal_type', '')),
                'signal_source_text': str(sig.get('source_text', '')),
                'signal_source_url': str(sig.get('source_url', '')),
                'archived': bool(int(row.get('archived', 0))),
            }
        )
    return out


def _strategy_trade_pnl_stats(strategy_ids: list[str]) -> dict[str, dict[str, Any]]:
    ids = [str(x or '').strip() for x in strategy_ids if str(x or '').strip()]
    if not ids:
        return {}
    uniq = list(dict.fromkeys(ids))
    for sid in uniq:
        try:
            _materialize_strategy_trades(sid, limit=5000)
        except Exception:
            continue

    day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    placeholders = ','.join(['?'] * len(uniq))
    rows = quant_db.fetch_all(
        f"""
        SELECT
          strategy_id,
          COALESCE(SUM(pnl), 0) AS total_pnl,
          COALESCE(SUM(CASE WHEN timestamp >= ? THEN pnl ELSE 0 END), 0) AS today_pnl,
          COUNT(1) AS trade_rows,
          SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS win_rows,
          SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) AS loss_rows
        FROM strategy_trades
        WHERE archived = 0 AND strategy_id IN ({placeholders})
        GROUP BY strategy_id
        """,
        tuple([day_start] + uniq),
    )
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = str(row.get('strategy_id', '')).strip()
        if not sid:
            continue
        wins = int(row.get('win_rows', 0) or 0)
        losses = int(row.get('loss_rows', 0) or 0)
        closed = wins + losses
        out[sid] = {
            'total_pnl': _safe_float(row.get('total_pnl', 0.0)),
            'today_pnl': _safe_float(row.get('today_pnl', 0.0)),
            'trade_rows': int(row.get('trade_rows', 0) or 0),
            'wins': wins,
            'losses': losses,
            'win_rate': (wins / closed) if closed > 0 else 0.0,
        }
    return out


def _strategy_runtime_rows(include_orphans: bool = False) -> list[dict[str, Any]]:
    _migrate_workshop_strategies_once()
    cfg_rows = strategy_store.load_strategies()
    cfg_map = {x.strategy_id: x for x in cfg_rows}
    registry_rows = quant_db.list_strategies(limit=5000, include_archived=True)
    registry_map = {str(x.get('id', '')): x for x in registry_rows if isinstance(x, dict)}
    paper = paper_engine.status(limit=5000)
    leaderboard = paper.get('leaderboard', []) if isinstance(paper, dict) else []
    perf_rows = LivePerformanceService(strategy_store.read_logs(limit=5000)).compute()
    perf_map = {x.strategy_id: x for x in perf_rows}
    quant_perf_map = {
        str(x.get('strategy_id', '')): x
        for x in quant_db.strategy_performance(mode='paper', hours=0)
        if isinstance(x, dict)
    }
    risk_map = {str(x.get('strategy_id', '')): x for x in quant_db.list_strategy_risk() if isinstance(x, dict)}
    acct_risk = quant_db.account_risk()
    now = datetime.now(timezone.utc)
    q_status = quant_orchestrator.status()
    q_cfg = q_status.get('config', {}) if isinstance(q_status, dict) else {}
    q_running = bool(q_status.get('running', False)) if isinstance(q_status, dict) else False
    q_started_at_utc = str(q_status.get('started_at_utc', '')) if isinstance(q_status, dict) else ''
    q_started_dt = _parse_iso_utc(q_started_at_utc)
    account_stop_reason = str(acct_risk.get('stop_reason', '')).strip()

    quant_sid_meta = {
        'arb_detector': {'name': 'Arbitrage Detector', 'strategy_type': 'arb', 'source': 'quant'},
        'market_maker': {'name': 'Market Maker', 'strategy_type': 'mm', 'source': 'quant'},
        'ai_probability': {'name': 'AI Probability', 'strategy_type': 'ai', 'source': 'quant'},
    }
    ids: set[str] = set(cfg_map.keys())
    ids.update(quant_sid_meta.keys())
    if include_orphans:
        if isinstance(leaderboard, list):
            for row in leaderboard:
                if isinstance(row, dict):
                    sid = str(row.get('strategy_id', '')).strip()
                    if sid:
                        ids.add(sid)
        ids.update(perf_map.keys())
        ids.update(risk_map.keys())
    trade_pnl_map = _strategy_trade_pnl_stats(sorted(ids))

    lb_map = {}
    if isinstance(leaderboard, list):
        for row in leaderboard:
            if isinstance(row, dict):
                sid = str(row.get('strategy_id', '')).strip()
                if sid:
                    lb_map[sid] = row

    out: list[dict[str, Any]] = []
    for sid in sorted(ids):
        cfg = cfg_map.get(sid)
        registry = registry_map.get(sid, {})
        lb = lb_map.get(sid, {})
        perf = perf_map.get(sid)
        risk = risk_map.get(sid, {})
        qperf = quant_perf_map.get(sid, {})
        enabled = bool(cfg.enabled) if cfg is not None else True
        status = 'running' if enabled else 'stopped'
        paused_until = _parse_iso_utc((risk or {}).get('paused_until_utc', ''))
        pause_reason = ''
        stop_reason = ''
        reg_status = str((registry or {}).get('status', '')).strip().lower()
        reg_stop_reason = str((registry or {}).get('stop_reason', '')).strip()
        running_since_utc = ''
        runtime_hours = 0.0

        first_fill_utc = ''
        if isinstance(qperf, dict):
            first_fill_utc = str(qperf.get('first_fill_utc', '')).strip()
            runtime_hours = max(runtime_hours, _safe_float(qperf.get('runtime_hours', 0.0)))
        first_fill_dt = _parse_iso_utc(first_fill_utc)
        if first_fill_dt is not None:
            running_since_utc = first_fill_utc
            runtime_hours = max(runtime_hours, _hours_since(first_fill_dt, now))
        elif cfg is not None:
            created_dt = _parse_iso_utc(cfg.created_at_utc)
            if created_dt is not None:
                running_since_utc = cfg.created_at_utc
                runtime_hours = max(runtime_hours, _hours_since(created_dt, now))

        if paused_until is not None and paused_until > now:
            status = 'paused'
            pause_reason = f"风控暂停至 {paused_until.isoformat()}"
            consecutive_losses = int((risk or {}).get('consecutive_losses', 0))
            if consecutive_losses > 0:
                pause_reason = f"{pause_reason}（连续亏损 {consecutive_losses} 笔）"
        elif reg_status == 'paused':
            status = 'paused'
            pause_reason = reg_stop_reason or '手动暂停'
        elif reg_status in {'stopped', 'archived'} and status != 'paused':
            status = 'stopped'
            stop_reason = reg_stop_reason or '手动停止'

        trades = int(lb.get('trade_count', 0)) if isinstance(lb, dict) else 0
        win_rate = float(lb.get('win_rate', 0.0)) if isinstance(lb, dict) else 0.0
        max_dd_pct = float(lb.get('max_drawdown_pct', 0.0)) if isinstance(lb, dict) else 0.0
        total_pnl = float(lb.get('total_pnl', 0.0)) if isinstance(lb, dict) else 0.0
        today_pnl = 0.0
        if perf is not None and trades <= 0:
            trades = int(perf.trades)
            win_rate = float(perf.win_rate)
            max_dd_pct = float(perf.max_drawdown_pct)
            total_pnl = float(perf.realized_pnl)
        tp = trade_pnl_map.get(sid, {})
        if isinstance(tp, dict) and tp:
            total_pnl = float(tp.get('total_pnl', total_pnl))
            today_pnl = float(tp.get('today_pnl', 0.0))
            trade_rows = int(tp.get('trade_rows', 0))
            if trade_rows > 0:
                trades = trade_rows
            win_rate = float(tp.get('win_rate', win_rate))

        if sid in quant_sid_meta:
            meta = quant_sid_meta[sid]
            cfg_name = str(meta.get('name', sid))
            cfg_type = str(meta.get('strategy_type', 'quant'))
            cfg_source = str(meta.get('source', 'quant'))
            if sid == 'arb_detector':
                enabled = bool(q_cfg.get('enable_arb', True))
            elif sid == 'market_maker':
                enabled = bool(q_cfg.get('enable_mm', True))
            elif sid == 'ai_probability':
                enabled = bool(q_cfg.get('enable_ai', True))
            status = 'running' if q_running and enabled else ('paused' if enabled else 'stopped')
            if qperf:
                trades = int(qperf.get('fills_count', 0))
                win_rate = float(qperf.get('win_rate', 0.0))
                max_dd_pct = 0.0
                total_pnl = float(qperf.get('pnl_total', 0.0))
            if status == 'running':
                if q_started_dt is not None:
                    running_since_utc = q_started_dt.isoformat()
                    runtime_hours = max(runtime_hours, _hours_since(q_started_dt, now))
            elif status == 'paused':
                if not pause_reason:
                    pause_reason = '量化引擎未启动（点击启动策略后会自动拉起）'
            elif status == 'stopped':
                stop_reason = '策略开关已关闭'
                if not bool(int(acct_risk.get('trading_enabled', 1))) and account_stop_reason:
                    stop_reason = account_stop_reason
            out.append(
                {
                    'strategy_id': sid,
                    'name': cfg_name,
                    'strategy_type': cfg_type,
                    'params': {},
                    'enabled': enabled,
                    'source': cfg_source,
                    'created_at_utc': '',
                    'status': status,
                    'total_pnl': total_pnl,
                    'today_pnl': today_pnl,
                    'win_rate': win_rate,
                    'trade_count': trades,
                    'max_drawdown_pct': max_dd_pct,
                    'equity': float(lb.get('equity', 0.0)) if isinstance(lb, dict) else 0.0,
                    'open_orders': int(lb.get('open_orders', 0)) if isinstance(lb, dict) else 0,
                    'runtime_hours': runtime_hours,
                    'running_since_utc': running_since_utc,
                    'pause_reason': pause_reason,
                    'stop_reason': stop_reason,
                }
            )
            continue

        if status == 'stopped' and not stop_reason:
            stop_reason = '手动停止' if cfg is not None and not cfg.enabled else '策略已停止'

        out.append(
            {
                'strategy_id': sid,
                'name': cfg.name if cfg is not None else sid,
                'strategy_type': cfg.strategy_type if cfg is not None else '',
                'params': cfg.params if cfg is not None else {},
                'enabled': enabled,
                'source': cfg.source if cfg is not None else 'runtime',
                'created_at_utc': cfg.created_at_utc if cfg is not None else '',
                'status': status,
                'total_pnl': total_pnl,
                'today_pnl': today_pnl,
                'win_rate': win_rate,
                'trade_count': trades,
                'max_drawdown_pct': max_dd_pct,
                'equity': float(lb.get('equity', 0.0)) if isinstance(lb, dict) else 0.0,
                'open_orders': int(lb.get('open_orders', 0)) if isinstance(lb, dict) else 0,
                'runtime_hours': runtime_hours,
                'running_since_utc': running_since_utc,
                'pause_reason': pause_reason,
                'stop_reason': stop_reason,
            }
        )
    out.sort(key=lambda x: float(x.get('total_pnl', 0.0)), reverse=True)
    try:
        _sync_strategy_registry(out)
    except Exception:
        pass
    return out


for _ in range(max(1, int(market_translate_worker_count))):
    threading.Thread(target=_market_translation_worker, daemon=True).start()


@app.get('/')
def index() -> FileResponse:
    dashboard_index = STATIC_DIR / 'dashboard' / 'index.html'
    if dashboard_index.exists():
        return FileResponse(str(dashboard_index))
    return FileResponse(str(STATIC_DIR / 'index.html'))


@app.get('/live')
def live_page() -> FileResponse:
    return FileResponse(str(STATIC_DIR / 'live.html'))


@app.get('/paper')
def paper_page() -> FileResponse:
    dashboard_index = STATIC_DIR / 'dashboard' / 'index.html'
    if dashboard_index.exists():
        return FileResponse(str(dashboard_index))
    return FileResponse(str(STATIC_DIR / 'paper_v2.html'))


@app.get('/dashboard')
def dashboard_page() -> FileResponse:
    dashboard_index = STATIC_DIR / 'dashboard' / 'index.html'
    if not dashboard_index.exists():
        raise HTTPException(status_code=404, detail='dashboard 未构建，请先执行 npm run build')
    return FileResponse(str(dashboard_index))


@app.get('/strategies')
def strategies_page() -> FileResponse:
    dashboard_index = STATIC_DIR / 'dashboard' / 'index.html'
    if not dashboard_index.exists():
        raise HTTPException(status_code=404, detail='dashboard 未构建，请先执行 npm run build')
    return FileResponse(str(dashboard_index))


@app.get('/history')
def history_page() -> FileResponse:
    dashboard_index = STATIC_DIR / 'dashboard' / 'index.html'
    if not dashboard_index.exists():
        raise HTTPException(status_code=404, detail='dashboard 未构建，请先执行 npm run build')
    return FileResponse(str(dashboard_index))


@app.get('/workshop')
def workshop_page() -> FileResponse:
    dashboard_index = STATIC_DIR / 'dashboard' / 'index.html'
    if not dashboard_index.exists():
        raise HTTPException(status_code=404, detail='dashboard 未构建，请先执行 npm run build')
    return FileResponse(str(dashboard_index))


@app.get('/strategy/{strategy_id}')
def strategy_page(strategy_id: str) -> FileResponse:
    _ = strategy_id
    dashboard_index = STATIC_DIR / 'dashboard' / 'index.html'
    if not dashboard_index.exists():
        raise HTTPException(status_code=404, detail='dashboard 未构建，请先执行 npm run build')
    return FileResponse(str(dashboard_index))


@app.get('/settings')
def settings_page() -> FileResponse:
    dashboard_index = STATIC_DIR / 'dashboard' / 'index.html'
    if not dashboard_index.exists():
        raise HTTPException(status_code=404, detail='dashboard 未构建，请先执行 npm run build')
    return FileResponse(str(dashboard_index))


@app.get('/quant')
def quant_page() -> FileResponse:
    return FileResponse(str(STATIC_DIR / 'quant.html'))


@app.get('/paper-legacy')
def paper_page_legacy() -> FileResponse:
    return FileResponse(str(STATIC_DIR / 'paper.html'))


@app.get('/api/health')
def health() -> dict[str, Any]:
    return {'ok': True, 'service': 'live-site'}


@app.get('/api/status')
def status() -> dict[str, Any]:
    q = quant_orchestrator.status()
    return {
        'live_trading_enabled': settings.live_trading_enabled,
        'live_force_ack': settings.live_force_ack,
        'live_max_order_usdc': settings.live_max_order_usdc,
        'has_private_key': bool(settings.live_private_key),
        'has_funder': bool(settings.live_funder),
        'has_api_creds': bool(settings.live_api_key and settings.live_api_secret and settings.live_api_passphrase),
        'host': settings.live_host,
        'chain_id': settings.live_chain_id,
        'signature_type': settings.live_signature_type,
        'paper_use_market_ws': settings.paper_use_market_ws,
        'market_ws_endpoint': settings.market_ws_endpoint,
        'quant_running': bool(q.get('running', False)),
        'quant_cycle': int(q.get('cycle', 0)),
        'quant_phase': str(q.get('phase', 'idle')),
    }


@app.get('/api/paper/latest-summary')
def paper_latest_summary() -> dict[str, Any]:
    paper_dir = settings.paper_dir
    files = sorted(paper_dir.glob('paper_summary_*.json'))
    if not files:
        return {}
    try:
        return json.loads(files[-1].read_text(encoding='utf-8'))
    except Exception:
        return {}


@app.get('/api/paper/latest-race')
def paper_latest_race() -> dict[str, Any]:
    race_path = settings.paper_dir / 'race' / 'race_latest.json'
    if not race_path.exists():
        return {}
    try:
        return json.loads(race_path.read_text(encoding='utf-8'))
    except Exception:
        return {}


@app.get('/api/paper/logs')
def paper_logs(limit: int = 300) -> Any:
    return {'count': max(0, limit), 'rows': strategy_store.read_logs(limit=limit)}


@app.get('/api/paper/model-dashboard')
def paper_model_dashboard(limit: int = 2000) -> Any:
    logs = strategy_store.read_logs(limit=max(100, min(5000, limit)))
    strategies = strategy_store.load_strategies()

    total_generates = 0
    openclaw_generates = 0
    openclaw_errors = 0
    last_generate_utc = ''
    last_openclaw_error = ''
    recent_model_events: list[dict[str, Any]] = []

    for row in logs:
        kind = str(row.get('kind', ''))
        if kind == 'strategies_generate':
            total_generates += 1
            if str(row.get('source', '')).lower() == 'openclaw':
                openclaw_generates += 1
            last_generate_utc = str(row.get('time_utc', last_generate_utc))
            recent_model_events.append(
                {
                    'time_utc': row.get('time_utc', ''),
                    'kind': kind,
                    'source': row.get('source', ''),
                    'count': row.get('count', 0),
                }
            )
        elif kind == 'openclaw_generate_error':
            openclaw_errors += 1
            last_openclaw_error = str(row.get('error', ''))
            recent_model_events.append(
                {
                    'time_utc': row.get('time_utc', ''),
                    'kind': kind,
                    'error': row.get('error', ''),
                }
            )
        elif kind == 'openclaw_health_check':
            recent_model_events.append(
                {
                    'time_utc': row.get('time_utc', ''),
                    'kind': kind,
                    'status': row.get('status', ''),
                    'detail': row.get('detail', ''),
                    'latency_ms': row.get('latency_ms', 0),
                }
            )
        elif kind.startswith('bot_') and row.get('strategy_id'):
            recent_model_events.append(
                {
                    'time_utc': row.get('time_utc', ''),
                    'kind': kind,
                    'strategy_id': row.get('strategy_id', ''),
                    'signal': row.get('signal', ''),
                }
            )

    source_count: dict[str, int] = {}
    enabled_count = 0
    for s in strategies:
        source = str(s.source or 'unknown').lower()
        source_count[source] = source_count.get(source, 0) + 1
        if s.enabled:
            enabled_count += 1

    recent_model_events = recent_model_events[-20:]

    health = OpenClawClient(
        endpoint=settings.openclaw_endpoint,
        timeout_sec=min(8.0, settings.openclaw_timeout_sec),
        retries=0,
    ).health_check()

    # Auto discover local model endpoints once if allocation is empty.
    alloc = model_router_store.load()
    if not alloc.providers:
        extra = [settings.openclaw_endpoint] if settings.openclaw_endpoint else []
        discovered = discover_local_providers(extra_endpoints=extra)
        if discovered:
            providers = [
                ModelProvider(
                    provider_id=str(x.get('provider_id', '')),
                    name=str(x.get('name', '')),
                    endpoint=normalize_provider_endpoint(
                        str(x.get('endpoint', '')),
                        adapter=str(x.get('adapter', 'openclaw_compatible')),
                        company=str(x.get('company', 'local')),
                    ),
                    adapter=str(x.get('adapter', 'openclaw_compatible')),
                    model=str(x.get('model', '')),
                    enabled=bool(x.get('enabled', True)),
                    weight=float(x.get('weight', 1.0)),
                    priority=int(x.get('priority', 100)),
                    company=str(x.get('company', 'local')),
                )
                for x in discovered
            ]
            model_router_store.save(ModelAllocation(mode='weighted', providers=providers))
            strategy_store.append_log({'kind': 'model_discover_auto', 'discovered': len(providers)})

    return {
        'openclaw_configured': bool(settings.openclaw_endpoint),
        'openclaw_endpoint': settings.openclaw_endpoint,
        'openclaw_timeout_sec': settings.openclaw_timeout_sec,
        'openclaw_health_ok': health.ok,
        'openclaw_health_status': health.status,
        'openclaw_health_latency_ms': health.latency_ms,
        'openclaw_health_detail': health.detail,
        'strategy_total': len(strategies),
        'strategy_enabled': enabled_count,
        'strategy_source_count': source_count,
        'generate_total': total_generates,
        'generate_openclaw': openclaw_generates,
        'openclaw_error_total': openclaw_errors,
        'last_generate_utc': last_generate_utc,
        'last_openclaw_error': last_openclaw_error,
        'recent_model_events': recent_model_events,
    }


@app.post('/api/paper/model-check')
def paper_model_check() -> Any:
    health = OpenClawClient(
        endpoint=settings.openclaw_endpoint,
        timeout_sec=min(8.0, settings.openclaw_timeout_sec),
        retries=0,
    ).health_check()
    strategy_store.append_log(
        {
            'kind': 'openclaw_health_check',
            'ok': health.ok,
            'status': health.status,
            'latency_ms': health.latency_ms,
            'detail': health.detail,
        }
    )
    return asdict(health)


@app.get('/api/paper/models/companies')
def paper_model_companies() -> Any:
    return {'count': len(company_presets()), 'rows': company_presets()}


@app.post('/api/paper/models/catalog')
def paper_models_catalog(payload: ModelCatalogIn) -> Any:
    company = str(payload.company or 'custom').strip().lower()
    adapter = str(payload.adapter or 'openai_compatible').strip().lower()
    if adapter != 'openai_compatible':
        raise HTTPException(status_code=400, detail='模型目录仅支持 openai_compatible 适配器')

    endpoint = normalize_provider_endpoint(payload.endpoint, adapter=adapter, company=company)
    if not endpoint:
        raise HTTPException(status_code=400, detail='endpoint 不能为空')

    key = str(payload.api_key or '').strip()
    if company == 'yunwu' and not key:
        raise HTTPException(status_code=400, detail='Yunwu 拉取模型目录需要 API Key')

    headers = normalize_extra_headers(payload.extra_headers)
    if company == 'openrouter':
        headers.setdefault('HTTP-Referer', 'http://127.0.0.1:8780')
        headers.setdefault('X-Title', 'Polymarket Quant Desk')

    try:
        data = fetch_openai_compatible_models(
            endpoint=endpoint,
            api_key=key,
            timeout_sec=min(20.0, settings.openclaw_timeout_sec),
            extra_headers=headers,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    rows = (data.get('rows', []) if isinstance(data, dict) else [])[: max(1, payload.limit)]
    return {
        'company': company,
        'adapter': adapter,
        'endpoint': endpoint,
        'models_endpoint': data.get('models_endpoint', ''),
        'count': len(rows),
        'rows': rows,
    }


@app.get('/api/paper/models')
def paper_models() -> Any:
    cfg = model_router_store.load()
    pool = _get_provider_pool_state()
    pool_rows = pool.get('rows', []) if isinstance(pool, dict) else []
    if not isinstance(pool_rows, list) or not pool_rows:
        pool = _recheck_provider_pool(reason='paper_models_view')
        pool_rows = pool.get('rows', []) if isinstance(pool, dict) else []
    pool_map: dict[str, dict[str, Any]] = {}
    for item in pool_rows if isinstance(pool_rows, list) else []:
        if not isinstance(item, dict):
            continue
        pid = str(item.get('provider_id', '')).strip()
        if pid:
            pool_map[pid] = item
    rows: list[dict[str, Any]] = []
    for p in cfg.providers or []:
        if not p.company:
            p.company = infer_company(p.endpoint, p.adapter)
        p.endpoint = normalize_provider_endpoint(p.endpoint, adapter=p.adapter, company=p.company)
        row = _provider_public_payload(p)
        st = pool_map.get(str(p.provider_id or '').strip(), {})
        if isinstance(st, dict):
            row['available'] = bool(st.get('available', False))
            row['health_status'] = str(st.get('status', ''))
            row['health_error'] = str(st.get('error', ''))
            row['latency_ms'] = int(st.get('latency_ms', 0))
        rows.append(row)
    if rows:
        model_router_store.save(ModelAllocation(mode=cfg.mode, providers=list(cfg.providers or [])))
    return {'mode': cfg.mode, 'providers': rows, 'provider_pool': pool}


@app.put('/api/paper/models')
def paper_models_update(payload: ModelConfigIn) -> Any:
    mode = str(payload.mode or 'weighted').lower().strip()
    if mode not in {'weighted', 'priority'}:
        raise HTTPException(status_code=400, detail='mode 仅支持 weighted 或 priority')
    mode = 'priority'

    old_cfg = model_router_store.load()
    prev_map = {p.provider_id: p for p in (old_cfg.providers or [])}
    providers: list[ModelProvider] = []
    for row in payload.providers:
        if not isinstance(row, dict):
            continue
        provider_id = str(row.get('provider_id', '')).strip()
        if not provider_id:
            continue
        prev = prev_map.get(provider_id)
        company = str(row.get('company', '')).strip().lower() or (prev.company if prev else '')
        adapter = str(row.get('adapter', '')).strip() or (prev.adapter if prev else '')
        if not adapter:
            adapter = 'openai_compatible'
        endpoint_raw = str(row.get('endpoint', '')).strip() or (prev.endpoint if prev else '')
        if not company:
            company = infer_company(endpoint_raw, adapter)
        endpoint = normalize_provider_endpoint(endpoint_raw, adapter=adapter, company=company)
        api_key_in = str(row.get('api_key', '')).strip()
        api_key = api_key_in if api_key_in else (prev.api_key if prev else '')
        extra_headers = normalize_extra_headers(row.get('extra_headers', prev.extra_headers if prev else {}))
        if company == 'openrouter':
            extra_headers.setdefault('HTTP-Referer', 'http://127.0.0.1:8780')
            extra_headers.setdefault('X-Title', 'Polymarket Quant Desk')

        providers.append(
            ModelProvider(
                provider_id=provider_id,
                name=str(row.get('name', '')).strip() or provider_id,
                endpoint=endpoint,
                adapter=adapter,
                model=str(row.get('model', '')).strip(),
                enabled=(False if provider_id == 'local-openai' else bool(row.get('enabled', True))),
                weight=float(row.get('weight', 1.0)),
                priority=int(row.get('priority', 100)),
                company=company,
                api_key=api_key,
                extra_headers=extra_headers,
            )
        )

    model_router_store.save(ModelAllocation(mode=mode, providers=providers))
    _recheck_provider_pool(reason='paper_models_update')
    strategy_store.append_log({'kind': 'model_allocation_update', 'mode': mode, 'count': len(providers)})
    return {'ok': True, 'mode': mode, 'count': len(providers)}


@app.post('/api/paper/models/discover')
def paper_models_discover() -> Any:
    extra = [settings.openclaw_endpoint] if settings.openclaw_endpoint else []
    discovered = discover_local_providers(extra_endpoints=extra)

    cfg = model_router_store.load()
    curr = {p.provider_id: p for p in (cfg.providers or [])}
    # Merge by endpoint first, keep existing IDs when possible.
    endpoint_to_id = {p.endpoint: p.provider_id for p in (cfg.providers or [])}
    next_idx = len(curr) + 1
    for row in discovered:
        ep = str(row.get('endpoint', '')).strip()
        if not ep:
            continue
        pid = endpoint_to_id.get(ep) or str(row.get('provider_id', f'local-{next_idx:03d}'))
        if pid not in curr:
            curr[pid] = ModelProvider(
                provider_id=pid,
                name=str(row.get('name', pid)),
                endpoint=ep,
                adapter=str(row.get('adapter', 'openclaw_compatible')),
                model=str(row.get('model', '')),
                enabled=bool(row.get('enabled', True)),
                weight=float(row.get('weight', 1.0)),
                priority=int(row.get('priority', 100 + next_idx)),
                company=str(row.get('company', infer_company(ep, str(row.get('adapter', ''))))),
                api_key='',
                extra_headers={},
            )
            next_idx += 1

    merged = list(curr.values())
    model_router_store.save(ModelAllocation(mode=cfg.mode, providers=merged))
    _recheck_provider_pool(reason='paper_models_discover')
    strategy_store.append_log({'kind': 'model_discover', 'discovered': len(discovered), 'total': len(merged)})
    return {'discovered': discovered, 'total': len(merged)}


@app.post('/api/paper/models/register')
def paper_models_register(payload: ModelProviderIn) -> Any:
    cfg = model_router_store.load()
    providers = list(cfg.providers or [])
    prev = None
    for p in providers:
        if p.provider_id == payload.provider_id.strip():
            prev = p
            break

    adapter = (payload.adapter or '').strip().lower() or (prev.adapter if prev else '')
    if not adapter:
        adapter = 'openai_compatible'
    company = (payload.company or '').strip().lower() or (prev.company if prev else '')
    endpoint_input = (payload.endpoint or '').strip() or (prev.endpoint if prev else '')
    if not company:
        company = infer_company(endpoint_input, adapter)
    ep = normalize_provider_endpoint(endpoint_input, adapter=adapter, company=company)
    if not ep:
        raise HTTPException(status_code=400, detail='endpoint 不能为空（可先选择公司自动带出）')

    key_in = (payload.api_key or '').strip()
    api_key = key_in if key_in else (prev.api_key if prev else '')
    extra_headers = normalize_extra_headers(payload.extra_headers or (prev.extra_headers if prev else {}))
    if company == 'openrouter':
        extra_headers.setdefault('HTTP-Referer', 'http://127.0.0.1:8780')
        extra_headers.setdefault('X-Title', 'Polymarket Quant Desk')

    replaced = False
    for i, p in enumerate(providers):
        if p.provider_id == payload.provider_id:
            providers[i] = ModelProvider(
                provider_id=payload.provider_id.strip(),
                name=payload.name.strip() or payload.provider_id.strip(),
                endpoint=ep,
                adapter=adapter,
                model=payload.model.strip(),
                enabled=(False if payload.provider_id.strip() == 'local-openai' else bool(payload.enabled)),
                weight=float(payload.weight),
                priority=int(payload.priority),
                company=company,
                api_key=api_key,
                extra_headers=extra_headers,
            )
            replaced = True
            break
    if not replaced:
        providers.append(
            ModelProvider(
                provider_id=payload.provider_id.strip(),
                name=payload.name.strip() or payload.provider_id.strip(),
                endpoint=ep,
                adapter=adapter,
                model=payload.model.strip(),
                enabled=(False if payload.provider_id.strip() == 'local-openai' else bool(payload.enabled)),
                weight=float(payload.weight),
                priority=int(payload.priority),
                company=company,
                api_key=api_key,
                extra_headers=extra_headers,
            )
        )
    model_router_store.save(ModelAllocation(mode=cfg.mode, providers=providers))
    _recheck_provider_pool(reason='paper_models_register')
    strategy_store.append_log(
        {
            'kind': 'model_register',
            'provider_id': payload.provider_id.strip(),
            'adapter': adapter,
            'model': payload.model.strip(),
            'endpoint': ep,
            'company': company,
            'has_api_key': bool(api_key),
        }
    )
    return {'ok': True, 'replaced': replaced, 'total': len(providers)}


@app.get('/api/workshop/providers')
def workshop_providers() -> Any:
    pool = _recheck_provider_pool(reason='workshop_providers')
    pool_rows = pool.get('rows', []) if isinstance(pool, dict) else []
    pool_map: dict[str, dict[str, Any]] = {}
    for item in pool_rows if isinstance(pool_rows, list) else []:
        if not isinstance(item, dict):
            continue
        pid = str(item.get('provider_id', '')).strip()
        if pid:
            pool_map[pid] = item

    cfg = model_router_store.load()
    rows: list[dict[str, Any]] = []
    for p in cfg.providers or []:
        if not p.company:
            p.company = infer_company(p.endpoint, p.adapter)
        p.endpoint = normalize_provider_endpoint(p.endpoint, adapter=p.adapter, company=p.company)
        row = _provider_public_payload(p)
        st = pool_map.get(str(p.provider_id or '').strip(), {})
        if isinstance(st, dict):
            row['available'] = bool(st.get('available', False))
            row['health_status'] = str(st.get('status', ''))
            row['health_error'] = str(st.get('error', ''))
            row['latency_ms'] = int(st.get('latency_ms', 0))
        rows.append(row)

    selected = _pick_available_provider_id(preferred_id='')
    return {
        'mode': cfg.mode,
        'count': len(rows),
        'providers': rows,
        'selected_provider_id': selected,
        'provider_pool': pool,
    }


@app.post('/api/workshop/chat')
def workshop_chat(payload: WorkshopChatIn) -> Any:
    _migrate_workshop_strategies_once()
    msgs = []
    for row in payload.messages[-20:]:
        role = str(row.role or '').strip().lower()
        content = str(row.content or '').strip()
        if role not in {'user', 'assistant'} or not content:
            continue
        msgs.append({'role': role, 'content': content})

    latest_user = ''
    for row in reversed(msgs):
        if row['role'] == 'user':
            latest_user = row['content']
            break

    draft = _workshop_force_executable_draft(payload.draft, user_text=latest_user)
    source = 'local_fallback'
    provider_id = ''
    selected_provider_id = str(payload.provider_id or '').strip()
    fallback_from_provider_id = ''
    fallback_to_provider_id = ''
    fallback_reason = ''
    llm_error = ''
    reply = ''
    format_error = False
    provider_errors: list[dict[str, Any]] = []

    if msgs:
        cfg = model_router_store.load()
        provider_map: dict[str, ModelProvider] = {}
        for p in cfg.providers or []:
            pid = str(p.provider_id or '').strip()
            if not pid or not str(p.endpoint or '').strip():
                continue
            provider_map[pid] = p

        pool = _get_provider_pool_state()
        pool_rows = pool.get('rows', []) if isinstance(pool, dict) else []
        if not isinstance(pool_rows, list) or not pool_rows:
            pool = _recheck_provider_pool(reason='workshop_chat')
            pool_rows = pool.get('rows', []) if isinstance(pool, dict) else []
        available_ids = [
            str(row.get('provider_id', '')).strip()
            for row in (pool_rows if isinstance(pool_rows, list) else [])
            if isinstance(row, dict) and bool(row.get('available', False)) and bool(row.get('enabled', False))
        ]
        available_ids = [x for x in available_ids if x in provider_map]
        if not available_ids:
            available_ids = [p.provider_id for p in _enabled_providers_sorted(cfg) if str(p.provider_id or '').strip() in provider_map]

        candidates: list[str] = []
        if selected_provider_id:
            if selected_provider_id in available_ids:
                candidates.append(selected_provider_id)
            else:
                fallback_from_provider_id = selected_provider_id
                fallback_reason = f'provider={selected_provider_id} 不可用，自动切换'
        for pid in available_ids:
            if pid not in candidates:
                candidates.append(pid)

        if candidates:
            for idx, pid in enumerate(candidates):
                provider = provider_map.get(pid)
                if provider is None:
                    continue
                try:
                    reply, draft_out = _workshop_call_provider(provider=provider, messages=msgs, draft=draft)
                    draft = _workshop_force_executable_draft(draft_out, user_text=latest_user)
                    source = f"llm:{provider.provider_id}"
                    provider_id = provider.provider_id
                    if idx > 0 or (fallback_from_provider_id and provider_id != selected_provider_id):
                        if not fallback_from_provider_id and selected_provider_id and selected_provider_id != provider_id:
                            fallback_from_provider_id = selected_provider_id
                        fallback_to_provider_id = provider_id
                        if not fallback_reason:
                            fallback_reason = f'provider={fallback_from_provider_id} 调用失败，自动切换'
                    break
                except Exception as exc:
                    err_text = str(exc)
                    provider_errors.append({'provider_id': pid, 'error': err_text})
                    llm_error = err_text
                    if 'strategy_json' in err_text:
                        format_error = True
                    continue
        if not provider_id:
            if provider_errors:
                llm_error = ' | '.join([f"{x['provider_id']}: {x['error']}" for x in provider_errors[:3]])
            if provider_errors and format_error:
                reply = '❌ 当前模型返回格式错误：缺少 `strategy_json` 代码块。请调整提示词后重试。'
                source = 'llm_error'
            elif provider_errors:
                reply = f'❌ LLM 调用失败：{llm_error}'
                source = 'llm_error'
            else:
                reply, draft = _workshop_apply_local_adjustment(latest_user, draft)
                source = 'local_fallback'
        else:
            source = source or f'llm:{provider_id}'
    else:
        reply = '请先输入你的策略想法。'

    strategy_store.append_log(
        {
            'kind': 'workshop_chat',
            'provider_id': provider_id,
            'source': source,
            'llm_error': llm_error,
            'user_text': latest_user[:300],
            'draft_type': draft.get('type', ''),
            'draft_name': draft.get('name', ''),
            'selected_provider_id': selected_provider_id,
            'fallback_from_provider_id': fallback_from_provider_id,
            'fallback_to_provider_id': fallback_to_provider_id,
            'fallback_reason': fallback_reason,
            'format_error': format_error,
        }
    )
    return {
        'ok': True,
        'provider_id': provider_id,
        'selected_provider_id': selected_provider_id,
        'fallback_from_provider_id': fallback_from_provider_id,
        'fallback_to_provider_id': fallback_to_provider_id,
        'fallback_reason': fallback_reason,
        'provider_errors': provider_errors,
        'source': source,
        'assistant': reply,
        'llm_error': llm_error,
        'format_error': format_error,
        'draft': draft,
        'updated_at_utc': _now_utc_iso(),
    }


@app.post('/api/workshop/deploy')
def workshop_deploy(payload: WorkshopDeployIn) -> Any:
    _migrate_workshop_strategies_once()
    draft = _workshop_force_executable_draft(payload.draft)
    rows = strategy_store.load_strategies()
    sid = _workshop_next_strategy_id([x.strategy_id for x in rows])
    runtime_type, runtime_params = _workshop_map_to_runtime(draft)

    cfg = StrategyConfig(
        strategy_id=sid,
        name=str(draft.get('name', sid)),
        strategy_type=runtime_type,
        params=runtime_params,
        enabled=True,
        source='workshop',
        created_at_utc=_now_utc_iso(),
    )
    rows.append(cfg)
    strategy_store.save_strategies(rows)
    quant_db.upsert_strategy(
        {
            'id': sid,
            'name': cfg.name,
            'config_json': cfg.params,
            'status': 'running',
            'created_at': cfg.created_at_utc,
            'stop_reason': '',
        }
    )
    version_row = _record_strategy_version(
        sid,
        _snapshot_strategy_config(cfg),
        note='workshop_deploy',
        created_by='workshop',
        source='deploy',
    )

    template_path = _workshop_write_signal_template(
        strategy_id=sid,
        draft=draft,
        runtime_type=runtime_type,
        runtime_params=runtime_params,
    )
    simulation = _ensure_paper_simulation_running(reason='workshop_deploy')
    strategy_store.append_log(
        {
            'kind': 'workshop_deploy',
            'strategy_id': sid,
            'name': cfg.name,
            'workshop_type': draft.get('type', ''),
            'runtime_type': runtime_type,
            'provider_id': str(payload.provider_id or ''),
            'template_file': str(template_path),
            'simulation': simulation,
        }
    )
    return {
        'ok': True,
        'strategy_id': sid,
        'detail_url': f'/strategy/{sid}',
        'strategy': asdict(cfg),
        'template_file': str(template_path),
        'version': version_row,
        'simulation': simulation,
        'message': '策略已部署到模拟盘',
    }


@app.get('/api/paper/markets')
def paper_markets(limit: int = 20, active: bool = True, closed: bool = False) -> Any:
    def _build_market_row(m: dict[str, Any]) -> dict[str, Any]:
        token_ids = extract_token_ids(m)
        outcomes = _parse_str_list(m.get('outcomes'))
        outcome_prices = _parse_str_list(m.get('outcomePrices'))
        outcome_rows: list[dict[str, Any]] = []
        for i, token_id in enumerate(token_ids):
            price_num: float | None = None
            if i < len(outcome_prices):
                px = _safe_float(outcome_prices[i], default=-1.0)
                if px >= 0:
                    price_num = px
            outcome_rows.append(
                {
                    'token_id': token_id,
                    'outcome': outcomes[i] if i < len(outcomes) else f'Outcome-{i + 1}',
                    'price': price_num,
                }
            )

        tick_size = m.get('orderPriceMinTickSize', m.get('minimum_tick_size'))
        min_size = m.get('orderMinSize', m.get('minimum_order_size'))
        fees_enabled = m.get('feesEnabled')
        fee_type = m.get('feeType')
        maker_fee = m.get('maker_base_fee')
        taker_fee = m.get('taker_base_fee')

        if fees_enabled is None:
            fees_enabled = (_safe_float(maker_fee) > 0) or (_safe_float(taker_fee) > 0)
        if fee_type is None:
            fee_type = ''
            if _safe_float(maker_fee) > 0 or _safe_float(taker_fee) > 0:
                fee_type = f"maker={_safe_float(maker_fee)} taker={_safe_float(taker_fee)}"

        row_active = bool(m.get('active', not bool(m.get('closed', False))))
        row_closed = bool(m.get('closed', not row_active))
        return {
            'id': m.get('id') or m.get('condition_id') or '',
            'question': m.get('question') or m.get('description') or '',
            'active': row_active,
            'closed': row_closed,
            'liquidity': m.get('liquidity', m.get('liquidity_num', '')),
            'endDate': m.get('endDate', m.get('end_date_iso', '')),
            'token_ids': token_ids,
            'outcomes': outcome_rows,
            'best_bid': _safe_float(m.get('bestBid')) if m.get('bestBid') is not None else None,
            'best_ask': _safe_float(m.get('bestAsk')) if m.get('bestAsk') is not None else None,
            'spread': _safe_float(m.get('spread')) if m.get('spread') is not None else None,
            'order_price_min_tick_size': _safe_float(tick_size) if tick_size is not None else None,
            'order_min_size': _safe_float(min_size) if min_size is not None else None,
            'fees_enabled': bool(fees_enabled) if fees_enabled is not None else None,
            'fee_type': str(fee_type or ''),
            'market_slug': m.get('slug', m.get('market_slug', '')),
        }

    errors: list[str] = []
    source = 'gamma'
    raw_rows: list[dict[str, Any]] = []

    try:
        c = _public_client()
        rows = c.list_markets(limit=max(1, min(limit, 200)), active=active, closed=closed)
        raw_rows = [x for x in rows if isinstance(x, dict)]
    except Exception as exc:
        errors.append(f'gamma={exc}')

    if not raw_rows:
        source = 'clob_fallback'
        try:
            c2 = _live_client()
            payload = c2.get_markets()
            rows2 = payload.get('data', []) if isinstance(payload, dict) else payload
            if not isinstance(rows2, list):
                rows2 = []
            raw_rows = [x for x in rows2 if isinstance(x, dict)]
        except Exception as exc:
            errors.append(f'clob={exc}')

    if raw_rows:
        out: list[dict[str, Any]] = []
        rule_updates: list[dict[str, Any]] = []
        for m in raw_rows:
            row = _build_market_row(m)
            if active and not row['active']:
                continue
            if not closed and row['closed']:
                continue
            out.append(row)

            for tid in row['token_ids']:
                rule_updates.append(
                    {
                        'token_id': tid,
                        'tick_size': row['order_price_min_tick_size'],
                        'min_size': row['order_min_size'],
                        'fees_enabled': row['fees_enabled'],
                        'fee_type': row['fee_type'],
                    }
                )

            if len(out) >= max(1, min(limit, 200)):
                break

        # Fallback数据可能没有可靠 active/closed 语义，若过滤后为空则放宽条件返回前N条。
        if not out:
            for m in raw_rows:
                row = _build_market_row(m)
                if not row['token_ids']:
                    continue
                out.append(row)
                for tid in row['token_ids']:
                    rule_updates.append(
                        {
                            'token_id': tid,
                            'tick_size': row['order_price_min_tick_size'],
                            'min_size': row['order_min_size'],
                            'fees_enabled': row['fees_enabled'],
                            'fee_type': row['fee_type'],
                        }
                    )
                if len(out) >= max(1, min(limit, 200)):
                    break

        if rule_updates:
            paper_engine.update_token_rules_bulk(rule_updates)
        _save_paper_markets_cache(out, source=source)
        result: dict[str, Any] = {
            'count': len(out),
            'rows': out,
            'source': source,
            'updated_at_utc': datetime.now(timezone.utc).isoformat(),
        }
        if errors:
            result['warning'] = ' | '.join(errors)
        return result

    cache = _load_paper_markets_cache()
    cache_rows = cache.get('rows', []) if isinstance(cache, dict) else []
    if isinstance(cache_rows, list) and cache_rows:
        return {
            'count': len(cache_rows),
            'rows': cache_rows[: max(1, min(limit, 200))],
            'source': 'cache',
            'stale': True,
            'warning': '实时市场接口不可用，当前返回本地缓存。',
            'errors': errors,
            'updated_at_utc': datetime.now(timezone.utc).isoformat(),
            'cache_updated_at_utc': cache.get('updated_at_utc', ''),
        }

    raise HTTPException(status_code=500, detail=' | '.join(errors) or '无法获取市场数据')


@app.get('/api/paper/orderbook/{token_id}')
def paper_orderbook(token_id: str) -> Any:
    try:
        c = _public_client()
        raw_book = c.get_orderbook(token_id)
        book = _normalize_orderbook_payload(raw_book)
        if isinstance(book, dict):
            paper_engine.on_book(token_id=token_id, book=book, source='paper_orderbook_poll')
            paper_bot_manager.ingest_book(token_id=token_id, book=book, source='paper_orderbook_poll')
        return book
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get('/api/paper/token-rule/{token_id}')
def paper_token_rule(token_id: str) -> Any:
    return {'token_id': token_id, 'rule': paper_engine.token_rule(token_id)}


@app.get('/api/paper/token-rules')
def paper_token_rules() -> Any:
    rows = paper_engine.list_token_rules()
    return {'count': len(rows), 'rows': rows}


@app.get('/api/paper/stream/status')
def paper_stream_status() -> Any:
    return asdict(paper_market_stream.status())


@app.post('/api/paper/stream/start')
def paper_stream_start(payload: PaperStreamStartIn) -> Any:
    assets = [str(x or '').strip() for x in (payload.assets_ids or [])]
    assets = [x for x in assets if x]
    paper_market_stream.start(
        assets_ids=assets,
        custom_feature_enabled=payload.custom_feature_enabled,
    )
    return {'ok': True, **asdict(paper_market_stream.status())}


@app.post('/api/paper/stream/stop')
def paper_stream_stop() -> Any:
    paper_market_stream.stop()
    return {'ok': True, **asdict(paper_market_stream.status())}


@app.post('/api/paper/stream/subscribe')
def paper_stream_subscribe(payload: PaperStreamSubIn) -> Any:
    assets = [str(x or '').strip() for x in (payload.assets_ids or [])]
    assets = [x for x in assets if x]
    if not assets:
        raise HTTPException(status_code=400, detail='assets_ids 不能为空')
    paper_market_stream.add_assets(assets)
    paper_market_stream.start(assets_ids=[])
    return {'ok': True, **asdict(paper_market_stream.status())}


@app.post('/api/paper/stream/unsubscribe')
def paper_stream_unsubscribe(payload: PaperStreamSubIn) -> Any:
    assets = [str(x or '').strip() for x in (payload.assets_ids or [])]
    assets = [x for x in assets if x]
    if not assets:
        raise HTTPException(status_code=400, detail='assets_ids 不能为空')
    paper_market_stream.remove_assets(assets)
    return {'ok': True, **asdict(paper_market_stream.status())}


@app.get('/api/paper/trading/status')
def paper_trading_status(limit: int = 50) -> Any:
    status = paper_engine.status(limit=max(1, min(limit, 300)))
    status['bot'] = asdict(paper_bot_manager.status())
    status['stream'] = asdict(paper_market_stream.status())
    return status


@app.post('/api/paper/trading/reset')
def paper_trading_reset(payload: PaperResetIn) -> Any:
    paper_bot_manager.stop()
    status = paper_engine.reset(initial_cash_per_strategy=payload.initial_cash)
    strategy_store.append_log(
        {
            'kind': 'paper_reset',
            'initial_cash': status.get('initial_cash_per_strategy', settings.paper_initial_cash),
        }
    )
    status['bot'] = asdict(paper_bot_manager.status())
    status['stream'] = asdict(paper_market_stream.status())
    return status


@app.post('/api/admin/reset-all-data')
def admin_reset_all_data(payload: ResetAllDataIn) -> Any:
    global workshop_migration_done
    if not payload.confirm:
        raise HTTPException(status_code=400, detail='请确认后再执行重置（confirm=true）')

    stop_status: dict[str, Any] = {}

    def _safe_stop(key: str, fn: Any) -> None:
        try:
            out = fn()
            stop_status[key] = {'ok': True, 'detail': out if isinstance(out, dict) else {}}
        except Exception as exc:
            stop_status[key] = {'ok': False, 'error': str(exc)}

    _safe_stop('paper_auto', paper_auto_manager.stop)
    _safe_stop('quant_orchestrator', quant_orchestrator.stop)
    _safe_stop('paper_bot', paper_bot_manager.stop)
    _safe_stop('live_bot', bot_manager.stop)
    _safe_stop('paper_stream', paper_market_stream.stop)

    paper_status = paper_engine.reset(initial_cash_per_strategy=payload.initial_cash)

    strategy_store.save_strategies([])
    if strategy_store.log_file.exists():
        strategy_store.log_file.unlink(missing_ok=True)

    cleanup_patterns = [
        (settings.paper_dir, 'paper_summary_*.json'),
        (settings.paper_dir, 'paper_fills_*.jsonl'),
        (settings.paper_dir / 'race', 'race_summary_*.json'),
        (settings.paper_dir / 'race', 'strategy_*_summary.json'),
        (settings.paper_dir / 'race', 'strategy_*_fills.jsonl'),
    ]
    cleanup_files = [
        paper_markets_cache_file,
        settings.paper_dir / 'latest_state.json',
        settings.paper_dir / 'report_latest.html',
        settings.paper_dir / 'race' / 'race_latest.json',
        settings.paper_dir / 'race' / 'race_report_latest.html',
        settings.paper_dir / 'deploy' / 'promotion_candidate.json',
        settings.paper_dir / 'live' / 'promotion_candidate_live.json',
    ]
    cleanup_dirs = [
        settings.paper_dir / 'live' / 'workshop_signals',
    ]
    removed_files: list[str] = []
    removed_dirs: list[str] = []
    cleanup_errors: list[str] = []

    for f in cleanup_files:
        try:
            if f.exists():
                f.unlink()
                removed_files.append(str(f))
        except Exception as exc:
            cleanup_errors.append(f'{f}: {exc}')

    for folder, pattern in cleanup_patterns:
        try:
            if not folder.exists():
                continue
            for f in folder.glob(pattern):
                try:
                    if f.is_file():
                        f.unlink()
                        removed_files.append(str(f))
                except Exception as exc:
                    cleanup_errors.append(f'{f}: {exc}')
        except Exception as exc:
            cleanup_errors.append(f'{folder}/{pattern}: {exc}')

    for d in cleanup_dirs:
        try:
            if d.exists():
                shutil.rmtree(d)
                removed_dirs.append(str(d))
        except Exception as exc:
            cleanup_errors.append(f'{d}: {exc}')

    with generate_jobs_lock:
        generate_jobs.clear()
        generate_jobs_order.clear()

    with workshop_migration_lock:
        workshop_migration_done = False

    db_reset = quant_db.reset_debug_data(clear_market_translations=bool(payload.clear_market_translations))
    return {
        'ok': True,
        'message': '所有调试数据已清空，系统已回到初始状态。',
        'stops': stop_status,
        'paper': paper_status,
        'db': db_reset,
        'removed_files_count': len(removed_files),
        'removed_dir_count': len(removed_dirs),
        'removed_files': removed_files,
        'removed_dirs': removed_dirs,
        'cleanup_errors': cleanup_errors,
        'updated_at_utc': _now_utc_iso(),
    }


@app.get('/api/paper/trading/orders')
def paper_trading_orders(limit: int = 200, strategy_id: str = '', open_only: bool = False) -> Any:
    rows = paper_engine.list_orders(limit=max(1, min(limit, 2000)), strategy_id=strategy_id, open_only=open_only)
    return {'count': len(rows), 'rows': rows}


@app.get('/api/paper/trading/fills')
def paper_trading_fills(limit: int = 200, strategy_id: str = '') -> Any:
    rows = paper_engine.list_fills(limit=max(1, min(limit, 5000)), strategy_id=strategy_id)
    return {'count': len(rows), 'rows': rows}


@app.get('/api/paper/trading/positions')
def paper_trading_positions(strategy_id: str = '') -> Any:
    rows = paper_engine.list_positions(strategy_id=strategy_id)
    return {'count': len(rows), 'rows': rows}


@app.post('/api/paper/trading/orders/limit')
def paper_trading_order_limit(payload: PaperLimitOrderIn) -> Any:
    try:
        c = _public_client()
        book = c.get_orderbook(payload.token_id)
        out = paper_engine.place_limit_order(
            strategy_id=payload.strategy_id,
            token_id=payload.token_id,
            side=payload.side,
            price=payload.price,
            size=payload.size,
            order_type=payload.order_type,
            source='paper_manual',
            book=book if isinstance(book, dict) else None,
            expire_seconds=payload.expire_seconds,
            tick_size=payload.tick_size,
            min_size=payload.min_size,
            fee_bps=payload.fee_bps,
        )
        return out
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post('/api/paper/trading/orders/market')
def paper_trading_order_market(payload: PaperMarketOrderIn) -> Any:
    try:
        c = _public_client()
        book = c.get_orderbook(payload.token_id)
        out = paper_engine.place_market_order(
            strategy_id=payload.strategy_id,
            token_id=payload.token_id,
            side=payload.side,
            amount=payload.amount,
            order_type=payload.order_type,
            source='paper_manual',
            book=book if isinstance(book, dict) else None,
            fee_bps=payload.fee_bps,
        )
        return out
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post('/api/paper/trading/orders/cancel/{order_id}')
def paper_trading_order_cancel(order_id: str) -> Any:
    out = paper_engine.cancel_order(order_id=order_id)
    if not out.get('ok'):
        raise HTTPException(status_code=400, detail=out.get('reason', '撤单失败'))
    return out


@app.post('/api/paper/trading/orders/cancel-all')
def paper_trading_order_cancel_all() -> Any:
    return paper_engine.cancel_all()


@app.get('/api/paper/trading/bot/status')
def paper_trading_bot_status() -> Any:
    bot = asdict(paper_bot_manager.status())
    return {**bot, 'bot': bot, 'stream': asdict(paper_market_stream.status())}


@app.post('/api/paper/trading/bot/start')
def paper_trading_bot_start(payload: PaperBotStartIn) -> Any:
    token_id = payload.token_id.strip()
    if not token_id:
        raise HTTPException(status_code=400, detail='token_id 不能为空')
    if payload.prefer_stream and settings.paper_use_market_ws:
        paper_market_stream.start(assets_ids=[token_id])
        paper_market_stream.add_assets([token_id])
    paper_bot_manager.start(token_id=token_id, interval_sec=payload.interval_sec, prefer_stream=payload.prefer_stream)
    bot = asdict(paper_bot_manager.status())
    return {'ok': True, **bot, 'bot': bot, 'stream': asdict(paper_market_stream.status())}


@app.post('/api/paper/trading/bot/stop')
def paper_trading_bot_stop() -> Any:
    paper_bot_manager.stop()
    bot = asdict(paper_bot_manager.status())
    return {'ok': True, **bot, 'bot': bot, 'stream': asdict(paper_market_stream.status())}


@app.get('/api/paper/strategy-detail')
def paper_strategy_detail(
    strategy_id: str,
    fills_limit: int = 80,
    orders_limit: int = 80,
    logs_limit: int = 200,
) -> Any:
    rows = strategy_store.load_strategies()
    target = None
    for row in rows:
        if row.strategy_id == strategy_id:
            target = row
            break
    if target is None:
        raise HTTPException(status_code=404, detail='strategy_id 不存在')

    fills = paper_engine.list_fills(limit=max(1, min(fills_limit, 2000)), strategy_id=strategy_id)
    orders = paper_engine.list_orders(limit=max(1, min(orders_limit, 2000)), strategy_id=strategy_id, open_only=False)
    logs_raw = strategy_store.read_logs(limit=max(200, min(5000, logs_limit * 12)))
    logs = [x for x in logs_raw if str(x.get('strategy_id', '')) == strategy_id][-max(1, min(logs_limit, 2000)) :]
    account = paper_engine.account_snapshot(strategy_id)
    return {
        'strategy': asdict(target),
        'account': account,
        'orders': orders,
        'fills': fills,
        'logs': logs,
    }


@app.get('/api/paper/workflow-status')
def paper_workflow_status() -> Any:
    strategies = strategy_store.load_strategies()
    enabled_strategies = [x for x in strategies if x.enabled]
    model_cfg = model_router_store.load()
    enabled_models = [x for x in (model_cfg.providers or []) if x.enabled and x.endpoint.strip()]
    paper = paper_engine.status(limit=300)
    bot = paper_bot_manager.status()
    stream = paper_market_stream.status()
    leaderboard = paper.get('leaderboard', []) if isinstance(paper, dict) else []
    profitable = 0
    if isinstance(leaderboard, list):
        for row in leaderboard:
            if not isinstance(row, dict):
                continue
            if float(row.get('total_pnl', 0.0)) > 0 and int(row.get('trade_count', 0)) >= 5:
                profitable += 1

    summary_files = sorted(settings.paper_dir.glob('paper_summary_*.json'))
    race_file = settings.paper_dir / 'race' / 'race_latest.json'
    promotion_file = settings.paper_dir / 'live' / 'promotion_candidate_live.json'
    fills_count = int(paper.get('fills_count', 0)) if isinstance(paper, dict) else 0

    steps = [
        {
            'key': 'overview',
            'title': '总览数据',
            'done': bool(summary_files) and race_file.exists(),
            'detail': f"summary={1 if summary_files else 0} race={'Y' if race_file.exists() else 'N'}",
        },
        {
            'key': 'models',
            'title': '模型路由',
            'done': len(enabled_models) > 0,
            'detail': f'enabled_models={len(enabled_models)}',
        },
        {
            'key': 'strategies',
            'title': '策略准备',
            'done': len(enabled_strategies) > 0,
            'detail': f"total={len(strategies)} enabled={len(enabled_strategies)}",
        },
        {
            'key': 'execution',
            'title': '模拟执行',
            'done': bot.running and fills_count > 0,
            'detail': (
                f"bot={'running' if bot.running else 'stopped'} fills={fills_count} "
                f"stream={'connected' if stream.connected else 'disconnected'} recv={stream.recv_total}"
            ),
        },
        {
            'key': 'evaluation',
            'title': '盈利评估',
            'done': profitable > 0,
            'detail': f'profitable_strategies={profitable}',
        },
        {
            'key': 'promotion',
            'title': '晋级确认',
            'done': promotion_file.exists(),
            'detail': f"promotion_file={'Y' if promotion_file.exists() else 'N'}",
        },
    ]

    next_action = '可进入实盘灰度。'
    if len(enabled_models) <= 0:
        next_action = '先在模型策略工坊配置 provider，或自动发现本地模型。'
    elif len(enabled_strategies) <= 0:
        next_action = '先生成并启用策略。'
    elif not bot.running:
        next_action = '启动模拟 Bot 开始交易。'
    elif bot.prefer_stream and not stream.connected:
        next_action = '已启用事件驱动但市场流未连接，请检查网络或 stream 配置。'
    elif fills_count < 20:
        next_action = '继续积累成交样本（建议至少 20 笔）。'
    elif profitable <= 0:
        next_action = '当前还没有稳定盈利策略，建议继续调参与赛马。'
    elif not promotion_file.exists():
        next_action = '已出现盈利策略，可在晋级门禁中批准候选策略。'

    auto_status = paper_auto_manager.status(limit_logs=1)
    quant_status = quant_orchestrator.status()
    return {
        'steps': steps,
        'next_action': next_action,
        'counts': {
            'enabled_models': len(enabled_models),
            'strategies': len(strategies),
            'enabled_strategies': len(enabled_strategies),
            'fills_count': fills_count,
            'profitable_strategies': profitable,
        },
        'bot': asdict(bot),
        'stream': asdict(stream),
        'auto_quant': {
            'running': bool(auto_status.get('running', False)),
            'phase': str(auto_status.get('phase', 'idle')),
            'cycle': int(auto_status.get('cycle', 0)),
            'last_error': str(auto_status.get('last_error', '')),
            'active_token_id': str(auto_status.get('active_token_id', '')),
            'active_strategy_id': str(auto_status.get('active_strategy_id', '')),
        },
        'quant_runtime': {
            'running': bool(quant_status.get('running', False)),
            'phase': str(quant_status.get('phase', 'idle')),
            'cycle': int(quant_status.get('cycle', 0)),
            'last_error': str(quant_status.get('last_error', '')),
        },
    }


@app.get('/api/paper/auto/status')
def paper_auto_status(limit_logs: int = 200) -> Any:
    return paper_auto_manager.status(limit_logs=max(1, min(limit_logs, 1000)))


@app.post('/api/paper/auto/start')
def paper_auto_start(payload: PaperAutoStartIn) -> Any:
    return paper_auto_manager.start(payload)


@app.post('/api/paper/auto/stop')
def paper_auto_stop() -> Any:
    return paper_auto_manager.stop()


@app.post('/api/paper/auto/run-once')
def paper_auto_run_once(payload: PaperAutoStartIn) -> Any:
    running = bool(paper_auto_manager.status(limit_logs=1).get('running', False))
    if running:
        raise HTTPException(status_code=409, detail='自动量化正在运行中，请先停止再执行单轮。')
    try:
        summary = paper_auto_manager.run_one_cycle(payload, stop_event=threading.Event())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {'ok': True, 'summary': summary, 'status': paper_auto_manager.status(limit_logs=120)}


def _apply_quant_risk_from_payload(payload: QuantStartIn) -> None:
    quant_risk_engine.max_order_usdc = float(payload.max_order_usdc)
    quant_risk_engine.max_total_exposure_usdc = float(payload.max_total_exposure_usdc)
    quant_risk_engine.strategy_daily_loss_limit = float(payload.strategy_daily_loss_limit)
    quant_risk_engine.account_daily_loss_limit = float(payload.account_daily_loss_limit)
    quant_risk_engine.loss_streak_limit = int(payload.loss_streak_limit)
    quant_risk_engine.reduced_size_scale = float(payload.reduced_size_scale)
    quant_risk_engine.race_enabled = bool(payload.race_enabled)
    quant_risk_engine.race_min_fills = int(payload.race_min_fills)
    quant_risk_engine.race_min_win_rate = float(payload.race_min_win_rate)
    quant_risk_engine.race_min_pnl = float(payload.race_min_pnl)
    quant_risk_engine.race_lookback_hours = int(payload.race_lookback_hours)


def _apply_quant_signal_from_payload(payload: QuantStartIn) -> None:
    quant_signal_engine.update_limits(
        arb_buy_threshold=float(payload.arb_buy_threshold),
        arb_sell_threshold=float(payload.arb_sell_threshold),
        fee_buffer=float(payload.fee_buffer),
        mm_liq_min=float(payload.mm_liq_min),
        mm_liq_max=float(payload.mm_liq_max),
        mm_min_spread=float(payload.mm_min_spread),
        mm_min_volume=float(payload.mm_min_volume),
        mm_min_depth_usdc=float(payload.mm_min_depth_usdc),
        mm_min_market_count=int(payload.mm_min_market_count),
        mm_target_market_count=int(payload.mm_target_market_count),
        mm_max_single_side_position_usdc=float(payload.mm_max_single_side_position_usdc),
        mm_max_position_per_market_usdc=float(payload.mm_max_position_per_market_usdc),
        mm_inventory_skew_strength=float(payload.mm_inventory_skew_strength),
        mm_allow_short_sell=bool(payload.mm_allow_short_sell),
        mm_taker_rebalance=bool(payload.mm_taker_rebalance),
        ai_deviation_threshold=float(payload.ai_deviation_threshold),
        ai_min_confidence=float(payload.ai_min_confidence),
        ai_eval_interval_sec=int(payload.ai_eval_interval_sec),
        ai_max_markets_per_cycle=int(payload.ai_max_markets_per_cycle),
    )


def _quant_params_payload() -> dict[str, Any]:
    q_status = quant_orchestrator.status()
    q_cfg = q_status.get('config', {}) if isinstance(q_status, dict) else {}
    return {
        'arb_buy_threshold': float(quant_signal_engine.arb_buy_threshold),
        'arb_sell_threshold': float(quant_signal_engine.arb_sell_threshold),
        'fee_buffer': float(quant_signal_engine.fee_buffer),
        'mm_liq_min': float(quant_signal_engine.mm_liq_min),
        'mm_liq_max': float(quant_signal_engine.mm_liq_max),
        'mm_min_spread': float(quant_signal_engine.mm_min_spread),
        'mm_min_volume': float(quant_signal_engine.mm_min_volume),
        'mm_min_depth_usdc': float(quant_signal_engine.mm_min_depth_usdc),
        'mm_min_market_count': int(quant_signal_engine.mm_min_market_count),
        'mm_target_market_count': int(quant_signal_engine.mm_target_market_count),
        'mm_max_single_side_position_usdc': float(quant_signal_engine.mm_max_single_side_position_usdc),
        'mm_max_position_per_market_usdc': float(quant_signal_engine.mm_max_position_per_market_usdc),
        'mm_inventory_skew_strength': float(quant_signal_engine.mm_inventory_skew_strength),
        'mm_allow_short_sell': bool(quant_signal_engine.mm_allow_short_sell),
        'mm_taker_rebalance': bool(quant_signal_engine.mm_taker_rebalance),
        'ai_deviation_threshold': float(quant_signal_engine.ai_deviation_threshold),
        'ai_min_confidence': float(quant_signal_engine.ai_min_confidence),
        'ai_eval_interval_sec': int(quant_signal_engine.ai_eval_interval_sec),
        'ai_max_markets_per_cycle': int(quant_signal_engine.ai_max_markets_per_cycle),
        'enable_arb': bool(q_cfg.get('enable_arb', True)),
        'enable_mm': bool(q_cfg.get('enable_mm', True)),
        'enable_ai': bool(q_cfg.get('enable_ai', True)),
        'updated_at_utc': datetime.now(timezone.utc).isoformat(),
    }


def _apply_quant_param_patch(payload: QuantParamUpdateIn) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    for field in (
        'arb_buy_threshold',
        'arb_sell_threshold',
        'fee_buffer',
        'mm_liq_min',
        'mm_liq_max',
        'mm_min_spread',
        'mm_min_volume',
        'mm_min_depth_usdc',
        'mm_min_market_count',
        'mm_target_market_count',
        'mm_max_single_side_position_usdc',
        'mm_max_position_per_market_usdc',
        'mm_inventory_skew_strength',
        'mm_allow_short_sell',
        'mm_taker_rebalance',
        'ai_deviation_threshold',
        'ai_min_confidence',
        'ai_eval_interval_sec',
        'ai_max_markets_per_cycle',
    ):
        value = getattr(payload, field)
        if value is not None:
            kwargs[field] = value
    if kwargs:
        quant_signal_engine.update_limits(**kwargs)

    changed_flags: dict[str, Any] = {}
    with quant_orchestrator._lock:
        if payload.enable_arb is not None:
            quant_orchestrator._cfg.enable_arb = bool(payload.enable_arb)
            changed_flags['enable_arb'] = bool(payload.enable_arb)
        if payload.enable_mm is not None:
            quant_orchestrator._cfg.enable_mm = bool(payload.enable_mm)
            changed_flags['enable_mm'] = bool(payload.enable_mm)
        if payload.enable_ai is not None:
            quant_orchestrator._cfg.enable_ai = bool(payload.enable_ai)
            changed_flags['enable_ai'] = bool(payload.enable_ai)

    if any(
        bool(x)
        for x in (
            payload.enable_arb is True,
            payload.enable_mm is True,
            payload.enable_ai is True,
        )
    ):
        _ensure_quant_running(trigger='quant_params_enable')

    out = _quant_params_payload()
    quant_db.insert_event(
        'quant_params_update',
        '量化策略参数已更新',
        {'updated_fields': sorted(list(kwargs.keys()) + list(changed_flags.keys())), 'params': out},
    )
    return out


def _to_quant_cfg(payload: QuantStartIn) -> OrchestratorConfig:
    return OrchestratorConfig(
        mode=str(payload.mode or 'paper').strip().lower(),
        cycle_sec=int(payload.cycle_sec),
        market_limit=int(payload.market_limit),
        max_books=int(payload.max_books),
        max_signals_per_cycle=int(payload.max_signals_per_cycle),
        provider_id=str(payload.provider_id or '').strip(),
        ai_prompt=str(payload.ai_prompt or ''),
        enable_arb=bool(payload.enable_arb),
        enable_mm=bool(payload.enable_mm),
        enable_ai=bool(payload.enable_ai),
        dry_run=bool(payload.dry_run),
        enforce_live_gate=bool(payload.enforce_live_gate),
        live_gate_min_hours=int(payload.live_gate_min_hours),
        live_gate_min_win_rate=float(payload.live_gate_min_win_rate),
        live_gate_min_pnl=float(payload.live_gate_min_pnl),
        live_gate_min_fills=int(payload.live_gate_min_fills),
    )


def _ensure_quant_running(trigger: str = 'auto', preferred_provider_id: str = '') -> dict[str, Any]:
    status = quant_orchestrator.status()
    if bool(status.get('running', False)):
        return {'ok': True, 'started': False, 'reason': 'already_running'}

    cfg_raw = status.get('config', {}) if isinstance(status, dict) else {}
    pick_provider = _pick_available_provider_id(preferred_id=str(preferred_provider_id or cfg_raw.get('provider_id', '')).strip())
    cfg = OrchestratorConfig(
        mode='paper',
        cycle_sec=max(2, int(cfg_raw.get('cycle_sec', 12) or 12)),
        market_limit=max(10, int(cfg_raw.get('market_limit', 120) or 120)),
        max_books=max(20, int(cfg_raw.get('max_books', 400) or 400)),
        max_signals_per_cycle=max(1, int(cfg_raw.get('max_signals_per_cycle', 16) or 16)),
        provider_id=pick_provider,
        ai_prompt=str(cfg_raw.get('ai_prompt', '') or ''),
        enable_arb=bool(cfg_raw.get('enable_arb', True)),
        enable_mm=bool(cfg_raw.get('enable_mm', True)),
        enable_ai=bool(cfg_raw.get('enable_ai', True)),
        dry_run=bool(cfg_raw.get('dry_run', False)),
        enforce_live_gate=bool(cfg_raw.get('enforce_live_gate', True)),
        live_gate_min_hours=max(1, int(cfg_raw.get('live_gate_min_hours', 72) or 72)),
        live_gate_min_win_rate=max(0.0, min(1.0, float(cfg_raw.get('live_gate_min_win_rate', 0.45) or 0.45))),
        live_gate_min_pnl=float(cfg_raw.get('live_gate_min_pnl', 0.0) or 0.0),
        live_gate_min_fills=max(1, int(cfg_raw.get('live_gate_min_fills', 20) or 20)),
    )
    if cfg.enable_ai:
        _run_llm_health_check(reason=f'{trigger}_auto_start_quant', provider_id=pick_provider)
    out = quant_orchestrator.start(cfg)
    quant_db.insert_event(
        'quant_auto_start',
        f'自动启动量化引擎（trigger={trigger}）',
        {'trigger': trigger, 'provider_id': pick_provider, 'started': bool(out.get('ok', False))},
    )
    return {'ok': bool(out.get('ok', False)), 'started': bool(out.get('ok', False)), 'reason': trigger, 'provider_id': pick_provider}


def _quant_live_gate_check(payload: QuantStartIn) -> dict[str, Any]:
    strategy_ids: list[str] = []
    if payload.enable_arb:
        strategy_ids.append('arb_detector')
    if payload.enable_mm:
        strategy_ids.append('market_maker')
    if payload.enable_ai:
        strategy_ids.append('ai_probability')
    gate = quant_db.live_gate_status(
        min_hours=int(payload.live_gate_min_hours),
        min_win_rate=float(payload.live_gate_min_win_rate),
        min_pnl=float(payload.live_gate_min_pnl),
        min_fills=int(payload.live_gate_min_fills),
        strategy_ids=strategy_ids,
    )
    return gate


@app.get('/api/quant/status')
def quant_status() -> Any:
    status = quant_orchestrator.status()
    cfg = status.get('config', {}) if isinstance(status, dict) else {}
    gate = quant_db.live_gate_status(
        min_hours=max(1, int(cfg.get('live_gate_min_hours', 72))),
        min_win_rate=max(0.0, min(1.0, float(cfg.get('live_gate_min_win_rate', 0.45)))),
        min_pnl=float(cfg.get('live_gate_min_pnl', 0.0)),
        min_fills=max(1, int(cfg.get('live_gate_min_fills', 20))),
        strategy_ids=['arb_detector', 'market_maker', 'ai_probability'],
    )
    status['live_gate'] = gate
    status['llm_health'] = _get_llm_health_state()
    return status


@app.get('/api/quant/params')
def quant_params() -> Any:
    return _quant_params_payload()


@app.post('/api/quant/params')
def quant_params_update(payload: QuantParamUpdateIn) -> Any:
    return {'ok': True, 'params': _apply_quant_param_patch(payload)}


@app.get('/api/llm/health')
def llm_health() -> Any:
    return _get_llm_health_state()


@app.get('/api/llm/providers')
def llm_providers() -> Any:
    pool = _get_provider_pool_state()
    rows = pool.get('rows', []) if isinstance(pool, dict) else []
    if not isinstance(rows, list) or not rows:
        pool = _recheck_provider_pool(reason='api_view')
    return pool


@app.post('/api/llm/providers/recheck')
def llm_providers_recheck() -> Any:
    pool = _recheck_provider_pool(reason='manual_recheck')
    picked = _pick_available_provider_id(preferred_id=str(pool.get('current_provider_id', '')))
    llm = _run_llm_health_check(reason='manual_recheck', provider_id=picked)
    return {'ok': bool(llm.get('ok', False)), 'provider_pool': pool, 'llm_health': llm}


@app.post('/api/llm/health-check')
def llm_health_check(payload: LlmHealthCheckIn) -> Any:
    _recheck_provider_pool(reason='manual_health_check')
    return _run_llm_health_check(reason='manual', provider_id=str(payload.provider_id or '').strip())


@app.post('/api/quant/start')
def quant_start(payload: QuantStartIn) -> Any:
    mode = str(payload.mode or 'paper').strip().lower()
    if mode == 'live':
        _guard_live(payload.confirm_live)
        if payload.enforce_live_gate:
            gate = _quant_live_gate_check(payload)
            if int(gate.get('eligible_count', 0)) <= 0:
                raise HTTPException(status_code=400, detail={'message': '未满足72h模拟盘晋级门槛，禁止启动实盘。', 'gate': gate})
    _apply_quant_risk_from_payload(payload)
    _apply_quant_signal_from_payload(payload)
    if payload.enable_ai:
        _run_llm_health_check(reason='quant_start', provider_id=str(payload.provider_id or '').strip())
    cfg = _to_quant_cfg(payload)
    return quant_orchestrator.start(cfg)


@app.post('/api/quant/stop')
def quant_stop() -> Any:
    return quant_orchestrator.stop()


@app.post('/api/quant/run-once')
def quant_run_once(payload: QuantStartIn) -> Any:
    running = bool(quant_orchestrator.status().get('running', False))
    if running:
        raise HTTPException(status_code=409, detail='自动量化调度器运行中，请先停止后再执行单轮。')
    mode = str(payload.mode or 'paper').strip().lower()
    if mode == 'live':
        _guard_live(payload.confirm_live)
        if payload.enforce_live_gate:
            gate = _quant_live_gate_check(payload)
            if int(gate.get('eligible_count', 0)) <= 0:
                raise HTTPException(status_code=400, detail={'message': '未满足72h模拟盘晋级门槛，禁止执行实盘轮次。', 'gate': gate})
    _apply_quant_risk_from_payload(payload)
    _apply_quant_signal_from_payload(payload)
    if payload.enable_ai:
        _run_llm_health_check(reason='quant_run_once', provider_id=str(payload.provider_id or '').strip())
    cfg = _to_quant_cfg(payload)
    try:
        summary = quant_orchestrator.run_once(cfg)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {'ok': True, 'summary': summary, 'status': quant_orchestrator.status()}


@app.post('/api/quant/refresh-markets')
def quant_refresh_markets(limit: int = 120, max_books: int = 400) -> Any:
    try:
        out = quant_market_data_engine.refresh(
            market_limit=max(10, min(limit, 2000)),
            max_books=max(20, min(max_books, 5000)),
        )
        return {'ok': True, **out}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get('/api/quant/markets')
def quant_markets(limit: int = 200) -> Any:
    rows = quant_db.list_markets(limit=max(1, min(limit, 2000)))
    return {'count': len(rows), 'rows': rows}


@app.get('/api/quant/books')
def quant_books(limit: int = 400) -> Any:
    rows = quant_db.list_books(limit=max(1, min(limit, 5000)))
    return {'count': len(rows), 'rows': rows}


@app.get('/api/markets/monitor')
def markets_monitor(limit: int = 80) -> Any:
    rows = quant_db.list_markets(limit=max(1, min(limit, 2000)))
    market_ids = [str(x.get('market_id', '')).strip() for x in rows if isinstance(x, dict) and str(x.get('market_id', '')).strip()]
    trans_map = quant_db.get_market_translations(market_ids)
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        market_id = str(row.get('market_id', '')).strip()
        if not market_id:
            continue
        name_en = str(row.get('question', '')).strip() or market_id
        name_zh, name_en = _resolve_market_name_zh_en(market_id, name_en, trans_map=trans_map)
        yes_mid = _safe_float(row.get('yes_mid', 0.0))
        no_mid = _safe_float(row.get('no_mid', 0.0))
        mid = (yes_mid + max(0.0, 1.0 - yes_mid)) / 2.0 if yes_mid > 0 else (yes_mid + no_mid) / 2.0
        yes_spread = _safe_float(row.get('yes_spread', 0.0))
        no_spread = _safe_float(row.get('no_spread', 0.0))
        spread = max(yes_spread, no_spread)
        volume = _safe_float(row.get('volume', 0.0))
        yes_depth = _safe_float(row.get('yes_depth_bid', 0.0)) + _safe_float(row.get('yes_depth_ask', 0.0))
        no_depth = _safe_float(row.get('no_depth_bid', 0.0)) + _safe_float(row.get('no_depth_ask', 0.0))
        depth_usdc = yes_depth * max(yes_mid, 0.01) + no_depth * max(no_mid, 0.01)
        yes_ask = _safe_float(row.get('yes_best_ask', 0.0))
        no_ask = _safe_float(row.get('no_best_ask', 0.0))
        yes_no_sum = yes_ask + no_ask if yes_ask > 0 and no_ask > 0 else _safe_float(row.get('yes_no_sum', 0.0))
        mm_opportunity = (
            spread >= float(quant_signal_engine.mm_min_spread)
            and volume >= float(quant_signal_engine.mm_min_volume)
            and depth_usdc >= float(quant_signal_engine.mm_min_depth_usdc)
        )
        arb_opportunity = (
            yes_no_sum > 0
            and (
                yes_no_sum < float(quant_signal_engine.arb_buy_threshold)
                or yes_no_sum > float(quant_signal_engine.arb_sell_threshold)
            )
        )
        out.append(
            {
                'market_id': market_id,
                'name': name_zh,
                'name_zh': name_zh,
                'name_en': name_en,
                'mid_price': mid,
                'yes_mid': yes_mid,
                'no_mid': no_mid,
                'spread': spread,
                'spread_pct': spread * 100.0,
                'volume_24h': volume,
                'depth_usdc': depth_usdc,
                'yes_no_sum': yes_no_sum,
                'mm_opportunity': mm_opportunity,
                'arb_opportunity': arb_opportunity,
                'updated_at_utc': str(row.get('updated_at_utc', '')),
            }
        )
    out.sort(
        key=lambda x: (
            1 if bool(x.get('mm_opportunity', False)) else 0,
            1 if bool(x.get('arb_opportunity', False)) else 0,
            float(x.get('spread', 0.0)),
            float(x.get('volume_24h', 0.0)),
        ),
        reverse=True,
    )
    return {
        'count': len(out),
        'rows': out,
        'thresholds': {
            'mm_min_spread': float(quant_signal_engine.mm_min_spread),
            'mm_min_volume': float(quant_signal_engine.mm_min_volume),
            'mm_min_depth_usdc': float(quant_signal_engine.mm_min_depth_usdc),
            'arb_buy_threshold': float(quant_signal_engine.arb_buy_threshold),
            'arb_sell_threshold': float(quant_signal_engine.arb_sell_threshold),
        },
    }


@app.get('/api/ai/evals')
def ai_evals(limit: int = 200) -> Any:
    lim = max(1, min(limit, 500))
    rows = quant_db.fetch_all(
        "SELECT * FROM q_ai_eval ORDER BY evaluated_at_utc DESC LIMIT ?",
        (lim,),
    )
    market_rows = quant_db.list_markets(limit=2000)
    market_map = {str(x.get('market_id', '')): x for x in market_rows if isinstance(x, dict)}
    trans_map = quant_db.get_market_translations([str(x.get('market_id', '')).strip() for x in market_rows if isinstance(x, dict)])
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        market_id = str(row.get('market_id', '')).strip()
        m = market_map.get(market_id, {})
        question_en = str(row.get('question', '')).strip() or str(m.get('question', '')).strip() or market_id
        question_zh, question_en = _resolve_market_name_zh_en(market_id, question_en, trans_map=trans_map)
        market_yes_mid = _safe_float(m.get('yes_mid', 0.0))
        ai_prob = _safe_float(row.get('probability', 0.0))
        conf = _safe_float(row.get('confidence', 0.0))
        deviation = abs(ai_prob - market_yes_mid) if market_yes_mid > 0 else 0.0
        triggered = conf >= float(quant_signal_engine.ai_min_confidence) and deviation >= float(quant_signal_engine.ai_deviation_threshold)
        out.append(
            {
                'market_id': market_id,
                'name': question_zh,
                'name_zh': question_zh,
                'name_en': question_en,
                'market_yes_mid': market_yes_mid,
                'ai_probability': ai_prob,
                'deviation': deviation,
                'confidence': conf,
                'triggered': triggered,
                'reason': str(row.get('reason', '')),
                'model': str(row.get('model', '')),
                'evaluated_at_utc': str(row.get('evaluated_at_utc', '')),
            }
        )
    return {'count': len(out), 'rows': out, 'llm_health': _get_llm_health_state(), 'provider_pool': _get_provider_pool_state()}


@app.get('/api/quant/signals')
def quant_signals(limit: int = 300) -> Any:
    rows = quant_db.list_signals(limit=max(1, min(limit, 5000)))
    return {'count': len(rows), 'rows': rows}


@app.get('/api/quant/orders')
def quant_orders(limit: int = 300) -> Any:
    rows = quant_db.list_orders(limit=max(1, min(limit, 5000)))
    return {'count': len(rows), 'rows': rows}


@app.get('/api/quant/fills')
def quant_fills(limit: int = 300) -> Any:
    rows = quant_db.list_fills(limit=max(1, min(limit, 5000)))
    return {'count': len(rows), 'rows': rows}


@app.get('/api/quant/performance')
def quant_performance(mode: str = 'paper', hours: int = 0) -> Any:
    mode_norm = str(mode or 'paper').strip().lower()
    if mode_norm not in {'paper', 'live'}:
        raise HTTPException(status_code=400, detail='mode 仅支持 paper 或 live')
    rows = quant_db.strategy_performance(mode=mode_norm, hours=max(0, int(hours)))
    return {'mode': mode_norm, 'hours': max(0, int(hours)), 'count': len(rows), 'rows': rows}


@app.get('/api/quant/live-gate')
def quant_live_gate(
    min_hours: int = 72,
    min_win_rate: float = 0.45,
    min_pnl: float = 0.0,
    min_fills: int = 20,
) -> Any:
    return quant_db.live_gate_status(
        min_hours=max(1, int(min_hours)),
        min_win_rate=max(0.0, min(1.0, float(min_win_rate))),
        min_pnl=float(min_pnl),
        min_fills=max(1, int(min_fills)),
        strategy_ids=['arb_detector', 'market_maker', 'ai_probability'],
    )


@app.get('/api/quant/events')
def quant_events(limit: int = 300) -> Any:
    rows = quant_db.list_events(limit=max(1, min(limit, 5000)))
    return {'count': len(rows), 'rows': rows}


@app.get('/api/quant/risk')
def quant_risk() -> Any:
    snap = quant_risk_engine.snapshot()
    snap['live_gate'] = quant_db.live_gate_status(
        min_hours=72,
        min_win_rate=0.45,
        min_pnl=0.0,
        min_fills=20,
        strategy_ids=['arb_detector', 'market_maker', 'ai_probability'],
    )
    return snap


@app.get('/api/markets')
def markets(limit: int = 50) -> dict[str, Any]:
    try:
        c = _live_client()
        data = c.get_markets()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    rows = data.get('data', []) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        rows = []
    return {'count': min(limit, len(rows)), 'rows': rows[: max(1, limit)]}


@app.get('/api/orderbook/{token_id}')
def orderbook(token_id: str) -> Any:
    try:
        c = _live_client()
        return _normalize_orderbook_payload(c.get_order_book(token_id))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get('/api/account/open-orders')
def open_orders(market: str = '', asset_id: str = '') -> Any:
    if not (settings.live_api_key and settings.live_api_secret and settings.live_api_passphrase):
        return {
            'ok': False,
            'disabled': True,
            'reason': '未配置 POLYMARKET_API_KEY / POLYMARKET_API_SECRET / POLYMARKET_API_PASSPHRASE',
            'rows': [],
        }
    try:
        c = _live_client()
        return c.get_orders(market=market, asset_id=asset_id)
    except Exception as exc:
        return {
            'ok': False,
            'error': str(exc),
            'rows': [],
            'market': market,
            'asset_id': asset_id,
        }


@app.get('/api/account/trades')
def account_trades(market: str = '', asset_id: str = '') -> Any:
    if not (settings.live_api_key and settings.live_api_secret and settings.live_api_passphrase):
        return {
            'ok': False,
            'disabled': True,
            'reason': '未配置 POLYMARKET_API_KEY / POLYMARKET_API_SECRET / POLYMARKET_API_PASSPHRASE',
            'rows': [],
        }
    try:
        c = _live_client()
        return c.get_trades(market=market, asset_id=asset_id)
    except Exception as exc:
        return {
            'ok': False,
            'error': str(exc),
            'rows': [],
            'market': market,
            'asset_id': asset_id,
        }


@app.get('/api/account/balance')
def account_balance() -> Any:
    if not (settings.live_api_key and settings.live_api_secret and settings.live_api_passphrase):
        return {
            'ok': False,
            'disabled': True,
            'reason': '未配置 POLYMARKET_API_KEY / POLYMARKET_API_SECRET / POLYMARKET_API_PASSPHRASE',
            'balance': None,
        }
    try:
        c = _live_client()
        return c.get_balance()
    except Exception as exc:
        return {
            'ok': False,
            'error': str(exc),
            'balance': None,
        }


@app.post('/api/orders/limit')
def place_limit(payload: LimitOrderIn) -> Any:
    _guard_live(payload.confirm_live)
    _check_notional(side=payload.side, price=payload.price, size=payload.size, amount=None)

    try:
        c = _live_client()
        resp = c.place_limit_order(
            token_id=payload.token_id,
            side=payload.side,
            price=payload.price,
            size=payload.size,
            order_type=payload.order_type,
        )
        strategy_store.append_log(
            {
                'kind': 'limit_order',
                'strategy_id': payload.strategy_id,
                'request': payload.model_dump(),
                'response': resp,
            }
        )
        return resp
    except (LiveClientError, Exception) as exc:
        strategy_store.append_log(
            {'kind': 'limit_order_error', 'strategy_id': payload.strategy_id, 'request': payload.model_dump(), 'error': str(exc)}
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post('/api/orders/market')
def place_market(payload: MarketOrderIn) -> Any:
    _guard_live(payload.confirm_live)
    _check_notional(side=payload.side, price=None, size=None, amount=payload.amount)

    try:
        c = _live_client()
        resp = c.place_market_order(
            token_id=payload.token_id,
            side=payload.side,
            amount=payload.amount,
            order_type=payload.order_type,
        )
        strategy_store.append_log(
            {
                'kind': 'market_order',
                'strategy_id': payload.strategy_id,
                'request': payload.model_dump(),
                'response': resp,
            }
        )
        return resp
    except (LiveClientError, Exception) as exc:
        strategy_store.append_log(
            {'kind': 'market_order_error', 'strategy_id': payload.strategy_id, 'request': payload.model_dump(), 'error': str(exc)}
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post('/api/orders/cancel/{order_id}')
def cancel_order(order_id: str, confirm_live: bool = False) -> Any:
    _guard_live(confirm_live)
    try:
        c = _live_client()
        resp = c.cancel(order_id)
        strategy_store.append_log({'kind': 'cancel_order', 'order_id': order_id, 'response': resp})
        return resp
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post('/api/orders/cancel-all')
def cancel_all(confirm_live: bool = False) -> Any:
    _guard_live(confirm_live)
    try:
        c = _live_client()
        resp = c.cancel_all()
        strategy_store.append_log({'kind': 'cancel_all', 'response': resp})
        return resp
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get('/api/strategies')
def list_strategies() -> Any:
    rows = _strategy_runtime_rows()
    recent_map = _recent_signals_by_strategy(limit=3000)
    llm = _get_llm_health_state()
    ai_warn = not bool(llm.get('ok', False))
    for row in rows:
        sid = str(row.get('strategy_id', '')).strip()
        row['recent_signals'] = recent_map.get(sid, [])
        if sid == 'ai_probability':
            row['llm_warning'] = ai_warn
            row['llm_status'] = llm
    return {'count': len(rows), 'rows': rows}


@app.get('/api/account/summary')
def account_summary() -> Any:
    paper = paper_engine.status(limit=5000)
    totals = (paper.get('totals', {}) if isinstance(paper, dict) else {}) or {}
    runtime_rows = _strategy_runtime_rows()
    active = sum(1 for x in runtime_rows if str(x.get('status', '')) == 'running')
    total = len(runtime_rows)
    today_pnl = sum(_safe_float(x.get('today_pnl', 0.0)) for x in runtime_rows if isinstance(x, dict))
    cumulative_pnl = sum(_safe_float(x.get('total_pnl', 0.0)) for x in runtime_rows if isinstance(x, dict))

    runtime_ids = {str(x.get('strategy_id', '')).strip() for x in runtime_rows if isinstance(x, dict)}
    leaderboard = paper.get('leaderboard', []) if isinstance(paper, dict) else []
    lb_initial_map: dict[str, float] = {}
    if isinstance(leaderboard, list):
        for row in leaderboard:
            if not isinstance(row, dict):
                continue
            sid = str(row.get('strategy_id', '')).strip()
            if not sid:
                continue
            lb_initial_map[sid] = _safe_float(row.get('initial_cash', 0.0))
    visible_initial = 0.0
    for sid in runtime_ids:
        visible_initial += lb_initial_map.get(sid, 0.0)
    if visible_initial <= 1e-9:
        visible_initial = _safe_float(totals.get('initial_cash', 0.0))
    balance_usdc = visible_initial + cumulative_pnl

    risk_status = 'normal'
    acct_risk = quant_db.account_risk()
    if not bool(int(acct_risk.get('trading_enabled', 1))):
        risk_status = 'danger'
    elif today_pnl <= float(quant_risk_engine.account_daily_loss_limit) * 0.7:
        risk_status = 'warning'

    events = quant_db.list_events(limit=400)
    alert_kinds = {'cycle_error', 'signal_failed', 'signal_blocked_live_gate', 'signal_blocked_race'}
    now = datetime.now(timezone.utc)
    day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    alerts = 0
    for row in events:
        if not isinstance(row, dict):
            continue
        kind = str(row.get('kind', ''))
        if kind not in alert_kinds:
            continue
        ts = _parse_iso_utc(row.get('time_utc'))
        if ts is None or ts < day_start:
            continue
        alerts += 1

    return {
        'balance_usdc': balance_usdc,
        'today_pnl': today_pnl,
        'active_strategies': active,
        'total_strategies': total,
        'risk_status': risk_status,
        'alerts_today': alerts,
        'updated_at_utc': datetime.now(timezone.utc).isoformat(),
    }


@app.get('/api/pnl/history')
def pnl_history(limit: int = 800) -> Any:
    rows = quant_db.list_fills(limit=max(1, min(limit, 5000)))
    rows = [x for x in rows if isinstance(x, dict)]
    rows.sort(key=lambda x: str(x.get('time_utc', '')))
    cum = 0.0
    history: list[dict[str, Any]] = []
    by_strategy: dict[str, float] = {}
    for row in rows:
        sid = str(row.get('strategy_id', '')).strip() or 'unknown'
        pnl_delta = float(row.get('pnl_delta', 0.0))
        cum += pnl_delta
        by_strategy[sid] = by_strategy.get(sid, 0.0) + pnl_delta
        history.append(
            {
                'time_utc': str(row.get('time_utc', '')),
                'value': cum,
                'delta': pnl_delta,
                'strategy_id': sid,
            }
        )
    strat_rows = [{'strategy_id': k, 'pnl': v} for k, v in by_strategy.items()]
    strat_rows.sort(key=lambda x: float(x.get('pnl', 0.0)), reverse=True)
    return {
        'count': len(history),
        'rows': history,
        'by_strategy': strat_rows,
        'updated_at_utc': datetime.now(timezone.utc).isoformat(),
    }


@app.get('/api/trades/recent')
def trades_recent(limit: int = 20) -> Any:
    lim = max(1, min(limit, 200))
    fills = quant_db.list_fills(limit=max(lim * 8, 200))
    market_map = _quant_token_market_map()
    market_map_en = _quant_token_market_map_en()
    rows: list[dict[str, Any]] = []
    signal_ids: set[int] = set()
    for row in fills:
        if not isinstance(row, dict):
            continue
        sid = row.get('signal_id')
        if sid is None:
            continue
        try:
            signal_ids.add(int(sid))
        except Exception:
            continue
    signal_map: dict[int, dict[str, Any]] = {}
    if signal_ids:
        ids = sorted(signal_ids)
        placeholders = ','.join(['?'] * len(ids))
        sig_rows = quant_db.fetch_all(
            f"SELECT id, signal_type, status, status_message, reason_json FROM q_signal WHERE id IN ({placeholders})",
            tuple(ids),
        )
        for row in sig_rows:
            if not isinstance(row, dict):
                continue
            try:
                sid = int(row.get('id', 0))
            except Exception:
                continue
            signal_map[sid] = row

    for row in fills:
        if len(rows) >= lim:
            break
        if not isinstance(row, dict):
            continue
        token_id = str(row.get('token_id', '')).strip()
        signal_id = row.get('signal_id')
        signal_row = {}
        try:
            if signal_id is not None:
                signal_row = signal_map.get(int(signal_id), {}) or {}
        except Exception:
            signal_row = {}
        reason_obj = {}
        if isinstance(signal_row, dict):
            try:
                reason_obj = json.loads(str(signal_row.get('reason_json', '{}')))
            except Exception:
                reason_obj = {}
        rows.append(
            {
                'time_utc': str(row.get('time_utc', '')),
                'strategy_id': str(row.get('strategy_id', '')),
                'side': str(row.get('side', '')).upper(),
                'market_name': market_map.get(token_id, token_id[:18]),
                'market_name_en': market_map_en.get(token_id, token_id[:18]),
                'token_id': token_id,
                'price': float(row.get('price', 0.0)),
                'quantity': float(row.get('size', 0.0)),
                'signal_type': str(signal_row.get('signal_type', '')),
                'signal_status': str(signal_row.get('status', '')),
                'decision_reason': _signal_reason_text(reason_obj, signal_type=str(signal_row.get('signal_type', ''))),
            }
        )

    if not rows:
        legacy_fills = paper_engine.list_fills(limit=lim)
        for row in legacy_fills[:lim]:
            if not isinstance(row, dict):
                continue
            token_id = str(row.get('token_id', '')).strip()
            rows.append(
                {
                    'time_utc': str(row.get('time_utc', '')),
                    'strategy_id': str(row.get('strategy_id', '')),
                    'side': str(row.get('side', '')).upper(),
                    'market_name': market_map.get(token_id, token_id[:18]),
                    'market_name_en': market_map_en.get(token_id, token_id[:18]),
                    'token_id': token_id,
                    'price': float(row.get('price', 0.0)),
                    'quantity': float(row.get('quantity', 0.0)),
                    'signal_type': '',
                    'signal_status': '',
                    'decision_reason': '',
                }
            )
    return {'count': len(rows), 'rows': rows}


@app.get('/api/strategy/{strategy_id}/overview')
def strategy_overview(strategy_id: str, insight_limit: int = 30, pnl_limit: int = 5000) -> Any:
    sid = str(strategy_id or '').strip()
    if not sid:
        raise HTTPException(status_code=400, detail='strategy_id 不能为空')
    rows = _strategy_runtime_rows(include_orphans=True)
    target = None
    for row in rows:
        if str(row.get('strategy_id', '')) == sid:
            target = row
            break
    if target is None:
        raise HTTPException(status_code=404, detail='strategy_id 不存在')

    pnl_pack = _build_strategy_pnl_series(sid, limit=max(100, min(pnl_limit, 20000)))
    metrics = pnl_pack.get('metrics', {}) if isinstance(pnl_pack, dict) else {}
    if isinstance(target, dict):
        if float(metrics.get('trade_count', 0)) <= 0:
            metrics['trade_count'] = int(target.get('trade_count', 0))
        if float(metrics.get('win_rate', 0.0)) <= 0 and int(target.get('trade_count', 0)) > 0:
            metrics['win_rate'] = float(target.get('win_rate', 0.0))
        if abs(float(metrics.get('total_pnl', 0.0))) <= 1e-12:
            metrics['total_pnl'] = float(target.get('total_pnl', 0.0))
        metrics['max_drawdown_pct'] = float(target.get('max_drawdown_pct', 0.0))
        metrics['runtime_hours'] = float(target.get('runtime_hours', 0.0))
        metrics['running_since_utc'] = str(target.get('running_since_utc', ''))

    trade_rows = _materialize_strategy_trades(sid, limit=220)
    trades = _format_strategy_trades(sid, trade_rows[:200])
    insights = _build_strategy_ai_insights(sid, limit=max(1, min(insight_limit, 120)))
    versions = _strategy_versions_payload(sid, limit=60)
    history_rows = quant_db.list_param_history(sid, limit=120)
    param_history: list[dict[str, Any]] = []
    for row in history_rows:
        if not isinstance(row, dict):
            continue
        change = {}
        try:
            change = json.loads(str(row.get('change_json', '{}')))
        except Exception:
            change = {}
        param_history.append(
            {
                'id': int(row.get('id', 0)),
                'changed_at': str(row.get('changed_at', '')),
                'changed_by': str(row.get('changed_by', '')),
                'note': str(row.get('note', '')),
                'change': change,
            }
        )
    return {
        'strategy': target,
        'metrics': metrics,
        'pnl_rows': pnl_pack.get('rows', []) if isinstance(pnl_pack, dict) else [],
        'trades': trades,
        'insights': insights,
        'param_history': param_history,
        'versions': versions,
        'updated_at_utc': _now_utc_iso(),
    }


@app.get('/api/strategy/{strategy_id}/insights')
def strategy_insights(strategy_id: str, limit: int = 30) -> Any:
    sid = str(strategy_id or '').strip()
    if not sid:
        raise HTTPException(status_code=400, detail='strategy_id 不能为空')
    rows = _build_strategy_ai_insights(sid, limit=max(1, min(limit, 120)))
    return {'strategy_id': sid, 'count': len(rows), 'rows': rows, 'updated_at_utc': _now_utc_iso()}


@app.get('/api/strategy/{strategy_id}/trades')
def strategy_trades(strategy_id: str, limit: int = 200) -> Any:
    sid = str(strategy_id or '').strip()
    if not sid:
        raise HTTPException(status_code=400, detail='strategy_id 不能为空')
    rows = _materialize_strategy_trades(sid, limit=max(1, min(limit, 2000)))
    out = _format_strategy_trades(sid, rows[: max(1, min(limit, 2000))])
    return {'strategy_id': sid, 'count': len(out), 'rows': out, 'updated_at_utc': _now_utc_iso()}


@app.get('/api/strategy/{strategy_id}/params')
def strategy_params(strategy_id: str) -> Any:
    sid = str(strategy_id or '').strip()
    if not sid:
        raise HTTPException(status_code=400, detail='strategy_id 不能为空')
    if sid in {'arb_detector', 'market_maker', 'ai_probability'}:
        params = _quant_params_payload()
        return {
            'strategy_id': sid,
            'params': params,
            'editable': True,
            'source': 'quant_params',
            'updated_at_utc': _now_utc_iso(),
        }
    rows = strategy_store.load_strategies()
    for row in rows:
        if row.strategy_id != sid:
            continue
        return {
            'strategy_id': sid,
            'params': row.params,
            'editable': True,
            'source': 'strategy_store',
            'updated_at_utc': _now_utc_iso(),
        }
    raise HTTPException(status_code=404, detail='strategy_id 不存在')


@app.post('/api/strategy/{strategy_id}/params')
def strategy_params_update(strategy_id: str, payload: StrategyParamUpdateIn) -> Any:
    sid = str(strategy_id or '').strip()
    if not sid:
        raise HTTPException(status_code=400, detail='strategy_id 不能为空')
    note = str(payload.note or '').strip()
    params = payload.params if isinstance(payload.params, dict) else {}

    if sid in {'arb_detector', 'market_maker', 'ai_probability'}:
        allowed = set(QuantParamUpdateIn.model_fields.keys())
        patch = {k: v for k, v in params.items() if k in allowed}
        if not patch:
            raise HTTPException(status_code=400, detail='未提供可更新的参数')
        before = _quant_params_payload()
        out = _apply_quant_param_patch(QuantParamUpdateIn(**patch))
        after = _quant_params_payload()
        diff = {k: {'before': before.get(k), 'after': after.get(k)} for k in patch.keys()}
        quant_db.insert_param_history(
            strategy_id=sid,
            change=diff,
            note=note or '参数更新',
            changed_by='ui',
            changed_at=_now_utc_iso(),
        )
        quant_db.upsert_strategy(
            {
                'id': sid,
                'name': sid,
                'config_json': out,
                'status': 'running',
                'created_at': _now_utc_iso(),
                'stop_reason': '',
            }
        )
        snap = _snapshot_builtin_strategy(sid)
        snap['params'] = out
        ver = _record_strategy_version(
            sid,
            snap,
            note=note or 'inline_param_update',
            created_by='ui',
            source='params',
        )
        return {'ok': True, 'strategy_id': sid, 'params': out, 'diff': diff, 'version': ver}

    rows = strategy_store.load_strategies()
    updated = None
    before_params: dict[str, Any] = {}
    for i, row in enumerate(rows):
        if row.strategy_id != sid:
            continue
        before_params = dict(row.params or {})
        merged = dict(before_params)
        merged.update(params)
        updated = StrategyConfig(
            strategy_id=row.strategy_id,
            name=row.name,
            strategy_type=row.strategy_type,
            params=merged,
            enabled=row.enabled,
            source=row.source,
            created_at_utc=row.created_at_utc,
        )
        rows[i] = updated
        break
    if updated is None:
        raise HTTPException(status_code=404, detail='strategy_id 不存在')

    strategy_store.save_strategies(rows)
    diff_keys = sorted(set(before_params.keys()) | set(updated.params.keys()))
    diff: dict[str, Any] = {}
    for k in diff_keys:
        if before_params.get(k) != updated.params.get(k):
            diff[k] = {'before': before_params.get(k), 'after': updated.params.get(k)}
    quant_db.insert_param_history(
        strategy_id=sid,
        change=diff,
        note=note or '参数更新',
        changed_by='ui',
        changed_at=_now_utc_iso(),
    )
    quant_db.upsert_strategy(
        {
            'id': sid,
            'name': updated.name,
            'config_json': updated.params,
            'status': 'running' if updated.enabled else 'stopped',
            'created_at': updated.created_at_utc,
            'stop_reason': '' if updated.enabled else '手动停止',
        }
    )
    ver = _record_strategy_version(
        sid,
        _snapshot_strategy_config(updated),
        note=note or 'inline_param_update',
        created_by='ui',
        source='params',
    )
    return {'ok': True, 'strategy_id': sid, 'params': updated.params, 'diff': diff, 'version': ver}


@app.get('/api/strategy/{strategy_id}/params/history')
def strategy_params_history(strategy_id: str, limit: int = 80) -> Any:
    sid = str(strategy_id or '').strip()
    if not sid:
        raise HTTPException(status_code=400, detail='strategy_id 不能为空')
    rows = quant_db.list_param_history(sid, limit=max(1, min(limit, 500)))
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        change = {}
        try:
            change = json.loads(str(row.get('change_json', '{}')))
        except Exception:
            change = {}
        out.append(
            {
                'id': int(row.get('id', 0)),
                'changed_at': str(row.get('changed_at', '')),
                'changed_by': str(row.get('changed_by', '')),
                'note': str(row.get('note', '')),
                'change': change,
            }
        )
    return {'strategy_id': sid, 'count': len(out), 'rows': out}


@app.get('/api/strategy/{strategy_id}/versions')
def strategy_versions(strategy_id: str, limit: int = 80) -> Any:
    sid = str(strategy_id or '').strip()
    if not sid:
        raise HTTPException(status_code=400, detail='strategy_id 不能为空')
    rows = _strategy_versions_payload(sid, limit=max(1, min(limit, 500)))
    return {'strategy_id': sid, 'count': len(rows), 'rows': rows, 'updated_at_utc': _now_utc_iso()}


@app.post('/api/strategy/{strategy_id}/rollback')
def strategy_rollback(strategy_id: str, payload: StrategyRollbackIn) -> Any:
    sid = str(strategy_id or '').strip()
    if not sid:
        raise HTTPException(status_code=400, detail='strategy_id 不能为空')
    version_row = quant_db.get_strategy_version(sid, int(payload.version_no))
    if not isinstance(version_row, dict):
        raise HTTPException(status_code=404, detail=f'未找到版本 v{int(payload.version_no)}')
    try:
        snapshot = json.loads(str(version_row.get('config_json', '{}')))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f'版本配置损坏: {exc}') from exc
    if not isinstance(snapshot, dict):
        raise HTTPException(status_code=400, detail='版本配置不是对象，无法回滚')

    extra_note = str(payload.note or '').strip()
    rollback_note = f"rollback_to_v{int(payload.version_no)}" + (f" | {extra_note}" if extra_note else '')

    if _is_builtin_quant_strategy(sid):
        params_obj = snapshot.get('params', {})
        if not isinstance(params_obj, dict):
            raise HTTPException(status_code=400, detail='版本中缺少 params，无法回滚')
        before = _quant_params_payload()
        allowed = set(QuantParamUpdateIn.model_fields.keys())
        patch = {k: v for k, v in params_obj.items() if k in allowed}
        if not patch:
            raise HTTPException(status_code=400, detail='版本参数为空或不包含可回滚字段')
        out = _apply_quant_param_patch(QuantParamUpdateIn(**patch))

        enabled = bool(snapshot.get('enabled', True))
        with quant_orchestrator._lock:
            if sid == 'arb_detector':
                quant_orchestrator._cfg.enable_arb = enabled
            elif sid == 'market_maker':
                quant_orchestrator._cfg.enable_mm = enabled
            elif sid == 'ai_probability':
                quant_orchestrator._cfg.enable_ai = enabled

        quant_db.insert_param_history(
            strategy_id=sid,
            change={'before': before, 'after': out, 'rollback_from_version': int(payload.version_no)},
            note=rollback_note,
            changed_by='rollback',
            changed_at=_now_utc_iso(),
        )
        quant_db.upsert_strategy(
            {
                'id': sid,
                'name': sid,
                'config_json': out,
                'status': 'running' if enabled else 'paused',
                'created_at': _now_utc_iso(),
                'stop_reason': '' if enabled else '回滚后暂停',
            }
        )
        snap_after = _snapshot_builtin_strategy(sid)
        snap_after['params'] = out
        snap_after['enabled'] = enabled
        new_ver = _record_strategy_version(
            sid,
            snap_after,
            note=rollback_note,
            created_by='rollback',
            source='rollback',
        )
        strategy_store.append_log(
            {'kind': 'strategy_rollback', 'strategy_id': sid, 'version_no': int(payload.version_no), 'builtin': True}
        )
        return {
            'ok': True,
            'strategy_id': sid,
            'builtin': True,
            'rollback_from_version': int(payload.version_no),
            'new_version': new_ver,
            'params': out,
        }

    rows = strategy_store.load_strategies()
    idx = -1
    curr: StrategyConfig | None = None
    for i, row in enumerate(rows):
        if row.strategy_id == sid:
            idx = i
            curr = row
            break
    if idx < 0 or curr is None:
        raise HTTPException(status_code=404, detail='strategy_id 不存在或已删除，无法回滚')

    old_params = dict(curr.params or {})
    name = str(snapshot.get('name', curr.name)).strip() or curr.name
    st = str(snapshot.get('strategy_type', curr.strategy_type)).strip().lower() or curr.strategy_type
    if st not in {'periodic', 'mean_reversion'}:
        st = curr.strategy_type
    params = snapshot.get('params', curr.params)
    if not isinstance(params, dict):
        params = curr.params
    enabled = bool(snapshot.get('enabled', curr.enabled))
    source = str(snapshot.get('source', curr.source)).strip() or curr.source
    created_at = str(snapshot.get('created_at_utc', curr.created_at_utc)).strip() or curr.created_at_utc

    updated = StrategyConfig(
        strategy_id=sid,
        name=name,
        strategy_type=st,
        params=params,
        enabled=enabled,
        source=source,
        created_at_utc=created_at,
    )
    rows[idx] = updated
    strategy_store.save_strategies(rows)

    quant_db.insert_param_history(
        strategy_id=sid,
        change={'before': old_params, 'after': updated.params, 'rollback_from_version': int(payload.version_no)},
        note=rollback_note,
        changed_by='rollback',
        changed_at=_now_utc_iso(),
    )
    quant_db.upsert_strategy(
        {
            'id': sid,
            'name': updated.name,
            'config_json': updated.params,
            'status': 'running' if updated.enabled else 'paused',
            'created_at': updated.created_at_utc,
            'stop_reason': '' if updated.enabled else '回滚后暂停',
        }
    )
    new_ver = _record_strategy_version(
        sid,
        _snapshot_strategy_config(updated),
        note=rollback_note,
        created_by='rollback',
        source='rollback',
    )
    strategy_store.append_log(
        {'kind': 'strategy_rollback', 'strategy_id': sid, 'version_no': int(payload.version_no), 'builtin': False}
    )
    return {
        'ok': True,
        'strategy_id': sid,
        'builtin': False,
        'rollback_from_version': int(payload.version_no),
        'new_version': new_ver,
        'row': asdict(updated),
    }


@app.get('/api/strategy/{strategy_id}/delete-preview')
def strategy_delete_preview(strategy_id: str) -> Any:
    sid = str(strategy_id or '').strip()
    if not sid:
        raise HTTPException(status_code=400, detail='strategy_id 不能为空')
    overview = strategy_overview(strategy_id=sid, insight_limit=10, pnl_limit=5000)
    strategy = overview.get('strategy', {}) if isinstance(overview, dict) else {}
    metrics = overview.get('metrics', {}) if isinstance(overview, dict) else {}
    runtime_hours = _safe_float(metrics.get('runtime_hours', strategy.get('runtime_hours', 0.0)))
    return {
        'strategy_id': sid,
        'name': strategy.get('name', sid),
        'total_pnl': _safe_float(metrics.get('total_pnl', strategy.get('total_pnl', 0.0))),
        'trade_count': int(metrics.get('trade_count', strategy.get('trade_count', 0))),
        'runtime_days': runtime_hours / 24.0,
        'status': strategy.get('status', 'stopped'),
    }


@app.get('/api/history/strategies')
def history_strategies(limit: int = 200) -> Any:
    rows = quant_db.list_archived_strategies(limit=max(1, min(limit, 2000)))
    stat_rows = quant_db.fetch_all(
        """
        SELECT strategy_id, COUNT(1) AS trade_count, SUM(pnl) AS pnl_total
        FROM strategy_trades
        WHERE archived = 1
        GROUP BY strategy_id
        """
    )
    stat_map = {str(x.get('strategy_id', '')): x for x in stat_rows if isinstance(x, dict)}
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = str(row.get('id', ''))
        st = stat_map.get(sid, {})
        out.append(
            {
                'strategy_id': sid,
                'name': str(row.get('name', sid)),
                'status': str(row.get('status', 'archived')),
                'created_at': str(row.get('created_at', '')),
                'archived_at': str(row.get('archived_at', '')),
                'stop_reason': str(row.get('stop_reason', '')),
                'trade_count': int(st.get('trade_count', 0)),
                'total_pnl': _safe_float(st.get('pnl_total', 0.0)),
            }
        )
    return {'count': len(out), 'rows': out, 'updated_at_utc': _now_utc_iso()}


@app.get('/api/history/trades')
def history_trades(strategy_id: str = '', limit: int = 500) -> Any:
    rows = quant_db.list_archived_trades(strategy_id=strategy_id, limit=max(1, min(limit, 5000)))
    signal_ids: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = row.get('signal_id')
        try:
            if sid is not None:
                signal_ids.add(int(sid))
        except Exception:
            continue
    sig_map: dict[int, dict[str, Any]] = {}
    if signal_ids:
        placeholders = ','.join(['?'] * len(signal_ids))
        sig_rows = quant_db.fetch_all(
            f"SELECT source_signal_id, market_id, source_text FROM strategy_signals WHERE source_signal_id IN ({placeholders})",
            tuple(sorted(signal_ids)),
        )
        for row in sig_rows:
            if not isinstance(row, dict):
                continue
            try:
                key = int(row.get('source_signal_id', 0))
            except Exception:
                continue
            sig_map[key] = row
    market_ids = [str(x.get('market_id', '')).strip() for x in sig_map.values() if isinstance(x, dict) and str(x.get('market_id', '')).strip()]
    trans_map = quant_db.get_market_translations(market_ids)

    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        signal_id = row.get('signal_id')
        sig = {}
        try:
            if signal_id is not None:
                sig = sig_map.get(int(signal_id), {}) or {}
        except Exception:
            sig = {}
        market_id = str(sig.get('market_id', '')).strip()
        market_en = str(row.get('market', '')).strip() or str(sig.get('source_text', '')).strip()
        market_zh, market_en = _resolve_market_name_zh_en(market_id, market_en, trans_map=trans_map)
        out.append(
            {
                **row,
                'market': market_zh,
                'market_en': market_en,
                'market_id': market_id,
            }
        )
    return {'count': len(out), 'rows': out, 'updated_at_utc': _now_utc_iso()}


@app.post('/api/strategies/pause-all')
def strategies_pause_all() -> Any:
    rows = strategy_store.load_strategies()
    changed = 0
    new_rows: list[StrategyConfig] = []
    for row in rows:
        if row.enabled:
            changed += 1
        new_rows.append(
            StrategyConfig(
                strategy_id=row.strategy_id,
                name=row.name,
                strategy_type=row.strategy_type,
                params=row.params,
                enabled=False,
                source=row.source,
                created_at_utc=row.created_at_utc,
            )
        )
    if changed > 0 or rows:
        strategy_store.save_strategies(new_rows)
    for row in new_rows:
        quant_db.upsert_strategy(
            {
                'id': row.strategy_id,
                'name': row.name,
                'config_json': row.params,
                'status': 'paused',
                'created_at': row.created_at_utc,
                'stop_reason': '紧急一键暂停',
            }
        )

    with quant_orchestrator._lock:
        quant_orchestrator._cfg.enable_arb = False
        quant_orchestrator._cfg.enable_mm = False
        quant_orchestrator._cfg.enable_ai = False
    quant_db.set_strategy_status('arb_detector', 'paused', stop_reason='紧急一键暂停')
    quant_db.set_strategy_status('market_maker', 'paused', stop_reason='紧急一键暂停')
    quant_db.set_strategy_status('ai_probability', 'paused', stop_reason='紧急一键暂停')
    try:
        quant_orchestrator.stop()
    except Exception:
        pass
    try:
        paper_bot_manager.stop()
    except Exception:
        pass

    strategy_store.append_log({'kind': 'strategies_pause_all', 'changed': changed})
    quant_db.insert_event('strategies_pause_all', '一键暂停所有策略', {'changed': changed})
    return {'ok': True, 'changed': changed, 'quant_disabled': True}


@app.post('/api/strategies/cleanup-loser-stopped')
def strategies_cleanup_loser_stopped() -> Any:
    runtime_rows = _strategy_runtime_rows(include_orphans=False)
    quant_ids = {'arb_detector', 'market_maker', 'ai_probability'}
    losers = {
        str(x.get('strategy_id', '')).strip()
        for x in runtime_rows
        if isinstance(x, dict)
        and str(x.get('strategy_id', '')).strip()
        and str(x.get('status', '')).strip() == 'stopped'
        and float(x.get('total_pnl', 0.0)) < 0.0
        and str(x.get('strategy_id', '')).strip() not in quant_ids
    }
    all_rows = strategy_store.load_strategies()
    kept = [x for x in all_rows if x.strategy_id not in losers]
    if len(kept) != len(all_rows):
        strategy_store.save_strategies(kept)
    for sid in sorted(losers):
        _materialize_strategy_trades(sid, limit=2000)
        quant_db.archive_strategy(sid, stop_reason='清理亏损策略')

    strategy_store.append_log({'kind': 'strategies_cleanup_loser_stopped', 'removed': sorted(losers)})
    return {'ok': True, 'removed_count': len(losers), 'removed': sorted(losers), 'remaining': len(kept)}


@app.post('/api/strategy/{strategy_id}/delete')
def strategy_delete(strategy_id: str) -> Any:
    sid = str(strategy_id or '').strip()
    if not sid:
        raise HTTPException(status_code=400, detail='strategy_id 不能为空')
    if sid in {'arb_detector', 'market_maker', 'ai_probability'}:
        raise HTTPException(status_code=400, detail='内置量化策略不可删除')

    preview = strategy_delete_preview(sid)

    rows = strategy_store.load_strategies()
    exists = any(x.strategy_id == sid for x in rows)
    if not exists:
        raise HTTPException(status_code=404, detail='strategy_id 不存在')

    closed_orders = 0
    open_orders = paper_engine.list_orders(limit=5000, strategy_id=sid, open_only=True)
    for row in open_orders:
        if not isinstance(row, dict):
            continue
        oid = str(row.get('order_id', '')).strip()
        if not oid:
            continue
        try:
            paper_engine.cancel_order(oid)
            closed_orders += 1
        except Exception:
            continue

    _materialize_strategy_trades(sid, limit=5000)
    _materialize_strategy_signals(sid, limit=5000)
    quant_db.archive_strategy(sid, stop_reason='手动删除')

    kept = [x for x in rows if x.strategy_id != sid]
    strategy_store.save_strategies(kept)
    strategy_store.append_log({'kind': 'strategy_delete', 'strategy_id': sid, 'closed_orders': closed_orders})
    return {'ok': True, 'strategy_id': sid, 'remaining': len(kept), 'closed_orders': closed_orders, 'preview': preview}


@app.get('/api/strategies/export.csv')
def strategies_export_csv() -> Response:
    rows = _strategy_runtime_rows(include_orphans=True)
    body = _strategy_rows_csv(rows)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    headers = {'Content-Disposition': f'attachment; filename="strategies_{ts}.csv"'}
    return Response(content=body, media_type='text/csv; charset=utf-8', headers=headers)


@app.get('/api/strategy/{strategy_id}/export.csv')
def strategy_export_csv(strategy_id: str) -> Response:
    sid = str(strategy_id or '').strip()
    if not sid:
        raise HTTPException(status_code=400, detail='strategy_id 不能为空')
    rows = [x for x in _strategy_runtime_rows(include_orphans=True) if str(x.get('strategy_id', '')) == sid]
    if not rows:
        raise HTTPException(status_code=404, detail='strategy_id 不存在')
    body = _strategy_rows_csv(rows)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    headers = {'Content-Disposition': f'attachment; filename="strategy_{sid}_{ts}.csv"'}
    return Response(content=body, media_type='text/csv; charset=utf-8', headers=headers)


@app.post('/api/strategy/{strategy_id}/start')
def strategy_start(strategy_id: str) -> Any:
    payload = StrategyUpdateIn(strategy_id=strategy_id, enabled=True)
    out = toggle_strategy(payload)
    sid = str(strategy_id or '').strip()
    if sid:
        quant_db.set_strategy_status(sid, 'running', stop_reason='')
    return out


@app.post('/api/strategy/{strategy_id}/stop')
def strategy_stop(strategy_id: str) -> Any:
    payload = StrategyUpdateIn(strategy_id=strategy_id, enabled=False)
    out = toggle_strategy(payload)
    sid = str(strategy_id or '').strip()
    if sid:
        quant_db.set_strategy_status(sid, 'paused', stop_reason='手动暂停')
    return out


def _generate_strategies_impl(
    payload: StrategyGenerateIn,
    progress_hook: Any | None = None,
) -> dict[str, Any]:
    def _progress(stage: str, pct: int, message: str) -> None:
        if progress_hook is None:
            return
        try:
            progress_hook(stage, pct, message)
        except Exception:
            return

    rows = []
    selected_provider = None
    selected_adapter = ''
    selected_model = ''
    selected_company = ''
    used_fallback = False
    generation_error = ''
    generation_message = ''
    explicit_provider = str(payload.provider_id or '').strip()
    _progress('route_select', 5, '读取模型路由配置')
    cfg = model_router_store.load()
    provider = choose_provider(cfg, provider_id=payload.provider_id)
    if explicit_provider and provider is None:
        raise HTTPException(status_code=400, detail=f'provider_id 不存在或未启用: {explicit_provider}')
    if provider is not None:
        selected_provider = provider.provider_id
        selected_adapter = provider.adapter
        selected_model = provider.model
        selected_company = provider.company or infer_company(provider.endpoint, provider.adapter)
        _progress('model_request', 30, f'调用模型 provider={selected_provider}')
        try:
            rows = generate_model_strategies(
                endpoint=provider.endpoint,
                count=payload.count,
                timeout_sec=settings.openclaw_timeout_sec,
                adapter=provider.adapter,
                model=provider.model,
                api_key=provider.api_key,
                extra_headers=normalize_extra_headers(provider.extra_headers),
                company=selected_company,
                prompt=payload.prompt,
            )
            _progress('model_response', 72, f'模型返回 {len(rows)} 条策略')
        except Exception as exc:
            generation_error = str(exc)
            strategy_store.append_log(
                {
                    'kind': 'model_generate_error',
                    'provider_id': provider.provider_id,
                    'endpoint': provider.endpoint,
                    'adapter': provider.adapter,
                    'company': selected_company,
                    'error': generation_error,
                }
            )
            rows = []
        if not rows and not generation_error:
            generation_error = '模型返回空策略，请调整 prompt、model 或 provider 配置。'
            strategy_store.append_log(
                {
                    'kind': 'model_generate_empty',
                    'provider_id': provider.provider_id,
                    'endpoint': provider.endpoint,
                    'adapter': provider.adapter,
                    'company': selected_company,
                }
            )
        if generation_error and explicit_provider:
            _progress('failed', 100, '模型调用失败（显式 provider 禁止静默回退）')
            raise HTTPException(
                status_code=502,
                detail={
                    'error': generation_error,
                    'provider_id': provider.provider_id,
                    'adapter': provider.adapter,
                    'model': provider.model,
                    'company': selected_company,
                    'hint': '已禁用静默模板回退，请修复模型配置后重试。',
                },
            )
    elif settings.openclaw_endpoint:
        try:
            rows = generate_model_strategies(
                endpoint=settings.openclaw_endpoint,
                count=payload.count,
                timeout_sec=settings.openclaw_timeout_sec,
                adapter='openclaw_compatible',
                prompt=payload.prompt,
            )
            _progress('model_response', 72, f'默认模型返回 {len(rows)} 条策略')
        except Exception as exc:
            generation_error = str(exc)
            strategy_store.append_log({'kind': 'openclaw_generate_error', 'error': str(exc)})
            rows = []
        if not rows and not generation_error:
            generation_error = '默认模型返回空策略。'
    if not rows:
        if not payload.allow_fallback:
            _progress('failed', 100, '模型不可用，且已禁用模板回退')
            raise HTTPException(
                status_code=502,
                detail={
                    'error': generation_error or '模型不可用且已禁用模板回退。',
                    'provider_id': selected_provider,
                    'adapter': selected_adapter,
                    'model': selected_model,
                    'company': selected_company,
                },
            )
        _progress('fallback', 80, '模型不可用，回退模板策略')
        rows = generate_template_strategies(n=payload.count, seed=payload.seed)
        used_fallback = True
        generation_message = generation_error or '未命中可用模型，已使用模板策略。'
    _progress('save', 90, f'保存策略 {len(rows)} 条')
    strategy_store.save_strategies(rows)
    strategy_store.append_log(
        {
            'kind': 'strategies_generate',
            'count': len(rows),
            'seed': payload.seed,
            'prompt': payload.prompt[:600] if payload.prompt else '',
            'source': rows[0].source if rows else 'none',
            'provider_id': selected_provider or '',
            'adapter': selected_adapter,
            'model': selected_model,
            'company': selected_company,
            'used_fallback': used_fallback,
            'message': generation_message,
        }
    )
    out = {
        'count': len(rows),
        'rows': [asdict(s) for s in rows],
        'provider_id': selected_provider,
        'adapter': selected_adapter,
        'model': selected_model,
        'company': selected_company,
        'source': rows[0].source if rows else '',
        'used_fallback': used_fallback,
        'message': generation_message,
    }
    _progress('done', 100, f"策略生成完成 source={out.get('source', '-')}")
    return out


def _generate_strategies_job_runner(job_id: str, payload: StrategyGenerateIn) -> None:
    _job_set(
        job_id,
        status='running',
        stage='init',
        progress_pct=2,
        message='任务开始执行',
        event='任务开始执行',
    )

    def _hook(stage: str, pct: int, message: str) -> None:
        _job_set(
            job_id,
            status='running',
            stage=stage,
            progress_pct=pct,
            message=message,
            event=message,
        )

    try:
        result = _generate_strategies_impl(payload, progress_hook=_hook)
        preview_rows = []
        for row in result.get('rows', [])[:12]:
            if not isinstance(row, dict):
                continue
            preview_rows.append(
                {
                    'strategy_id': row.get('strategy_id', ''),
                    'name': row.get('name', ''),
                    'strategy_type': row.get('strategy_type', ''),
                    'source': row.get('source', ''),
                    'enabled': bool(row.get('enabled', True)),
                }
            )
        _job_set(
            job_id,
            status='succeeded',
            stage='done',
            progress_pct=100,
            message='策略生成完成',
            result={
                'count': int(result.get('count', 0)),
                'provider_id': result.get('provider_id', ''),
                'company': result.get('company', ''),
                'model': result.get('model', ''),
                'source': result.get('source', ''),
                'used_fallback': bool(result.get('used_fallback', False)),
                'message': result.get('message', ''),
                'rows': preview_rows,
            },
            event='策略生成完成',
        )
    except HTTPException as exc:
        detail = exc.detail
        _job_set(
            job_id,
            status='failed',
            stage='failed',
            progress_pct=100,
            message='策略生成失败',
            error={
                'status_code': exc.status_code,
                'detail': detail if isinstance(detail, (dict, list, str)) else str(detail),
            },
            event=f'策略生成失败: {detail}',
        )
    except Exception as exc:
        _job_set(
            job_id,
            status='failed',
            stage='failed',
            progress_pct=100,
            message='策略生成失败',
            error={'status_code': 500, 'detail': str(exc)},
            event=f'策略生成失败: {exc}',
        )


@app.post('/api/strategies/generate')
def generate_strategies(payload: StrategyGenerateIn) -> Any:
    return _generate_strategies_impl(payload)


@app.post('/api/strategies/generate-async')
def generate_strategies_async(payload: StrategyGenerateIn) -> Any:
    job = _job_create(payload)
    job_id = str(job.get('job_id', ''))
    t = threading.Thread(target=_generate_strategies_job_runner, args=(job_id, payload), daemon=True)
    t.start()
    return {
        'ok': True,
        'job_id': job_id,
        'status': 'queued',
        'created_at_utc': job.get('created_at_utc', ''),
        'request': job.get('request', {}),
    }


@app.get('/api/strategies/generate-jobs/{job_id}')
def generate_strategies_job_detail(job_id: str) -> Any:
    row = _job_get(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail='job_id 不存在')
    return row


@app.get('/api/strategies/generate-jobs')
def generate_strategies_jobs(limit: int = 20) -> Any:
    lim = max(1, min(limit, 100))
    with generate_jobs_lock:
        ids = list(reversed(generate_jobs_order[-lim:]))
        rows = [generate_jobs.get(i) for i in ids]
    out: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            out.append(json.loads(json.dumps(row, ensure_ascii=False)))
    return {'count': len(out), 'rows': out}


@app.post('/api/strategies/toggle')
def toggle_strategy(payload: StrategyUpdateIn) -> Any:
    rows = strategy_store.load_strategies()
    updated = False
    for i, row in enumerate(rows):
        if row.strategy_id == payload.strategy_id:
            rows[i] = StrategyConfig(
                strategy_id=row.strategy_id,
                name=row.name,
                strategy_type=row.strategy_type,
                params=row.params,
                enabled=payload.enabled,
                source=row.source,
                created_at_utc=row.created_at_utc,
            )
            updated = True
            break
    if not updated:
        sid = str(payload.strategy_id or '').strip()
        quant_ids = {'arb_detector', 'market_maker', 'ai_probability'}
        if sid in quant_ids:
            with quant_orchestrator._lock:
                if sid == 'arb_detector':
                    quant_orchestrator._cfg.enable_arb = bool(payload.enabled)
                elif sid == 'market_maker':
                    quant_orchestrator._cfg.enable_mm = bool(payload.enabled)
                elif sid == 'ai_probability':
                    quant_orchestrator._cfg.enable_ai = bool(payload.enabled)
            quant_db.insert_event(
                'quant_strategy_toggle',
                f'量化策略开关变更: {sid}',
                {'strategy_id': sid, 'enabled': bool(payload.enabled)},
            )
            quant_db.upsert_strategy(
                {
                    'id': sid,
                    'name': sid,
                    'config_json': {},
                    'status': 'running' if payload.enabled else 'stopped',
                    'created_at': _now_utc_iso(),
                    'stop_reason': '' if payload.enabled else '手动停止',
                }
            )
            auto = {'ok': True, 'started': False, 'reason': 'disabled'}
            if payload.enabled:
                auto = _ensure_quant_running(trigger=f'strategy_start_{sid}')
            return {'ok': True, 'strategy_id': sid, 'enabled': bool(payload.enabled), 'quant_runtime': auto}
        raise HTTPException(status_code=404, detail='strategy_id 不存在')

    strategy_store.save_strategies(rows)
    strategy_store.append_log({'kind': 'strategy_toggle', 'strategy_id': payload.strategy_id, 'enabled': payload.enabled})
    for row in rows:
        if row.strategy_id == payload.strategy_id:
            quant_db.upsert_strategy(
                {
                    'id': row.strategy_id,
                    'name': row.name,
                    'config_json': row.params,
                    'status': 'running' if payload.enabled else 'stopped',
                    'created_at': row.created_at_utc,
                    'stop_reason': '' if payload.enabled else '手动停止',
                }
            )
            break
    return {'ok': True}


@app.post('/api/strategies/update')
def update_strategy(payload: StrategyEditIn) -> Any:
    rows = strategy_store.load_strategies()
    updated_row = None
    old_params: dict[str, Any] = {}
    for i, row in enumerate(rows):
        if row.strategy_id != payload.strategy_id:
            continue

        name = row.name
        if payload.name is not None:
            name = payload.name.strip() or row.name

        strategy_type = row.strategy_type
        if payload.strategy_type is not None:
            st = payload.strategy_type.strip().lower()
            if st not in {'periodic', 'mean_reversion'}:
                raise HTTPException(status_code=400, detail='strategy_type 仅支持 periodic 或 mean_reversion')
            strategy_type = st

        params = row.params
        if payload.params is not None:
            old_params = dict(row.params or {})
            params = payload.params

        enabled = row.enabled if payload.enabled is None else bool(payload.enabled)

        rows[i] = StrategyConfig(
            strategy_id=row.strategy_id,
            name=name,
            strategy_type=strategy_type,
            params=params,
            enabled=enabled,
            source=row.source,
            created_at_utc=row.created_at_utc,
        )
        updated_row = rows[i]
        break

    if updated_row is None:
        raise HTTPException(status_code=404, detail='strategy_id 不存在')

    strategy_store.save_strategies(rows)
    strategy_store.append_log(
        {
            'kind': 'strategy_update',
            'strategy_id': payload.strategy_id,
            'name': updated_row.name,
            'strategy_type': updated_row.strategy_type,
            'enabled': updated_row.enabled,
        }
    )
    quant_db.upsert_strategy(
        {
            'id': updated_row.strategy_id,
            'name': updated_row.name,
            'config_json': updated_row.params,
            'status': 'running' if updated_row.enabled else 'stopped',
            'created_at': updated_row.created_at_utc,
            'stop_reason': '' if updated_row.enabled else '手动停止',
        }
    )
    if payload.params is not None:
        quant_db.insert_param_history(
            strategy_id=updated_row.strategy_id,
            change={'before': old_params, 'after': updated_row.params},
            note='策略参数更新',
            changed_by='api',
            changed_at=_now_utc_iso(),
        )
    ver = _record_strategy_version(
        updated_row.strategy_id,
        _snapshot_strategy_config(updated_row),
        note='strategy_update',
        created_by='api',
        source='update',
    )
    return {'ok': True, 'row': asdict(updated_row), 'version': ver}


@app.get('/api/strategies/logs')
def strategy_logs(limit: int = 200) -> Any:
    return {'count': max(0, limit), 'rows': strategy_store.read_logs(limit=limit)}


@app.get('/api/performance/strategies')
def performance_strategies(limit: int = 1000) -> Any:
    logs = strategy_store.read_logs(limit=max(100, min(5000, limit)))
    rows = LivePerformanceService(logs).compute()
    return {'count': len(rows), 'rows': [asdict(r) for r in rows[: max(1, min(limit, 2000))]]}


@app.get('/api/performance/promotion')
def performance_promotion(
    min_pnl: float = 0.0,
    max_dd_pct: float = 1.5,
    min_trades: int = 20,
    min_win_rate: float = 0.45,
) -> Any:
    logs = strategy_store.read_logs(limit=5000)
    rows = LivePerformanceService(logs).compute()
    qualified = filter_promotion_candidates(
        rows=rows,
        min_pnl=min_pnl,
        max_dd_pct=max_dd_pct,
        min_trades=min_trades,
        min_win_rate=min_win_rate,
    )
    return {'count': len(qualified), 'rows': [asdict(r) for r in qualified]}


@app.post('/api/performance/promotion/approve')
def performance_promotion_approve(payload: PromotionApproveIn) -> Any:
    logs = strategy_store.read_logs(limit=5000)
    rows = LivePerformanceService(logs).compute()
    target = None
    for r in rows:
        if r.strategy_id == payload.strategy_id:
            target = r
            break
    if target is None:
        raise HTTPException(status_code=404, detail='strategy_id 不存在或无绩效记录')

    qualified = filter_promotion_candidates(
        rows=[target],
        min_pnl=payload.min_pnl,
        max_dd_pct=payload.max_dd_pct,
        min_trades=payload.min_trades,
        min_win_rate=payload.min_win_rate,
    )
    if not qualified:
        raise HTTPException(status_code=400, detail='策略未通过晋级门槛，请先继续观察。')

    out = settings.paper_dir / 'live' / 'promotion_candidate_live.json'
    thresholds = {
        'min_pnl': payload.min_pnl,
        'max_dd_pct': payload.max_dd_pct,
        'min_trades': payload.min_trades,
        'min_win_rate': payload.min_win_rate,
    }
    save_promotion_candidate(out, target, thresholds=thresholds)
    strategy_store.append_log({'kind': 'promotion_approve', 'strategy_id': payload.strategy_id, 'file': str(out)})
    return {'ok': True, 'file': str(out), 'strategy_id': payload.strategy_id}


@app.get('/api/bot/status')
def bot_status() -> Any:
    s = bot_manager.status()
    return {'running': s.running, 'token_id': s.token_id, 'interval_sec': s.interval_sec, 'tick': s.tick}


@app.post('/api/bot/start')
def bot_start(payload: BotStartIn, confirm_live: bool = False) -> Any:
    _guard_live(confirm_live)
    bot_manager.start(token_id=payload.token_id, interval_sec=payload.interval_sec)
    s = bot_manager.status()
    return {'ok': True, 'running': s.running, 'token_id': s.token_id, 'interval_sec': s.interval_sec}


@app.post('/api/bot/stop')
def bot_stop(confirm_live: bool = False) -> Any:
    _guard_live(confirm_live)
    bot_manager.stop()
    s = bot_manager.status()
    return {'ok': True, 'running': s.running}


threading.Thread(target=_ensure_quant_running, kwargs={'trigger': 'startup'}, daemon=True).start()
