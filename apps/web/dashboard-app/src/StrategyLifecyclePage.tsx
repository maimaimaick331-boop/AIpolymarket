import { useCallback, useEffect, useMemo, useState } from 'react';
import { Download, PauseCircle, RefreshCw, Trash2 } from 'lucide-react';
import { apiGet, apiPost, toLocalTime } from './lib/api';
import type { StrategyRow } from './types';

const BUILTIN_IDS = new Set(['arb_detector', 'market_maker', 'ai_probability']);

function formatRuntime(hours: number): string {
  const h = Math.max(0, Number(hours || 0));
  if (h >= 48) return `${(h / 24).toFixed(1)} 天`;
  if (h >= 1) return `${h.toFixed(1)} 小时`;
  return `${Math.round(h * 60)} 分钟`;
}

function statusDot(status: string): string {
  if (status === 'running') return 'bg-dashboard-good animate-pulseRing';
  if (status === 'paused') return 'bg-yellow-400';
  return 'bg-dashboard-bad';
}

interface ActionResp {
  ok: boolean;
  changed?: number;
  removed_count?: number;
}

function StrategyThumb({
  row,
  onDelete,
}: {
  row: StrategyRow;
  onDelete: (strategyId: string) => void;
}) {
  const pnl = Number(row.total_pnl || 0);
  const winPct = Math.max(0, Math.min(100, Number(row.win_rate || 0) * 100));
  const canDelete = row.status === 'stopped' && !BUILTIN_IDS.has(row.strategy_id);
  const reason = row.status === 'paused' ? row.pause_reason : row.status === 'stopped' ? row.stop_reason : '';

  return (
    <div className="card p-3 space-y-2">
      <button
        onClick={() => {
          window.location.href = `/strategy/${encodeURIComponent(row.strategy_id)}`;
        }}
        className="w-full text-left"
      >
        <div className="flex items-center justify-between">
          <div className="min-w-0">
            <div className="text-xs text-dashboard-muted font-mono truncate">{row.strategy_id}</div>
            <div className="font-semibold truncate">{row.name || row.strategy_id}</div>
          </div>
          <span className={`inline-block h-2.5 w-2.5 rounded-full ${statusDot(row.status)}`} />
        </div>
        <div className={`mt-2 text-lg font-semibold ${pnl >= 0 ? 'text-dashboard-good' : 'text-dashboard-bad'}`}>{pnl.toFixed(4)}</div>
        <div className="grid grid-cols-2 gap-2 text-xs text-dashboard-muted mt-1">
          <div>胜率: <span className="text-dashboard-text">{winPct.toFixed(1)}%</span></div>
          <div>交易: <span className="text-dashboard-text">{row.trade_count}</span></div>
          <div>运行时长: <span className="text-dashboard-text">{formatRuntime(Number(row.runtime_hours || 0))}</span></div>
          <div>来源: <span className="text-dashboard-text">{row.source || '-'}</span></div>
        </div>
        {reason ? <div className="mt-2 text-xs text-yellow-300">原因: {reason}</div> : null}
      </button>
      {canDelete ? (
        <button
          onClick={() => onDelete(row.strategy_id)}
          className="w-full rounded-lg border border-rose-500/50 bg-rose-500/10 px-3 py-1.5 text-sm text-rose-300 hover:border-rose-400 inline-flex items-center justify-center gap-1"
        >
          <Trash2 size={14} />
          删除
        </button>
      ) : null}
    </div>
  );
}

export default function StrategyLifecyclePage() {
  const [rows, setRows] = useState<StrategyRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshAt, setRefreshAt] = useState('');
  const [msg, setMsg] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const loadRows = useCallback(async () => {
    try {
      const out = await apiGet<{ rows: StrategyRow[] }>('/strategies');
      setRows(out.rows || []);
      setRefreshAt(new Date().toLocaleString('zh-CN', { hour12: false }));
      setError('');
    } catch (err) {
      setError((err as Error).message || '加载失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadRows();
    const timer = window.setInterval(() => {
      void loadRows();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [loadRows]);

  const grouped = useMemo(() => {
    const running = rows.filter((x) => x.status === 'running').sort((a, b) => Number(b.total_pnl || 0) - Number(a.total_pnl || 0));
    const paused = rows.filter((x) => x.status === 'paused').sort((a, b) => Number(b.total_pnl || 0) - Number(a.total_pnl || 0));
    const stopped = rows.filter((x) => x.status === 'stopped').sort((a, b) => Number(b.total_pnl || 0) - Number(a.total_pnl || 0));
    return { running, paused, stopped };
  }, [rows]);

  async function pauseAll() {
    if (busy) return;
    setBusy(true);
    try {
      const out = await apiPost<ActionResp>('/strategies/pause-all');
      setMsg(`已暂停所有策略，变更 ${out.changed ?? 0} 个`);
      await loadRows();
    } catch (err) {
      setError((err as Error).message || '暂停失败');
    } finally {
      setBusy(false);
    }
  }

  async function cleanupLosers() {
    if (busy) return;
    setBusy(true);
    try {
      const out = await apiPost<ActionResp>('/strategies/cleanup-loser-stopped');
      setMsg(`清理完成，删除 ${out.removed_count ?? 0} 个亏损已停止策略`);
      await loadRows();
    } catch (err) {
      setError((err as Error).message || '清理失败');
    } finally {
      setBusy(false);
    }
  }

  async function deleteStrategy(strategyId: string) {
    if (busy) return;
    if (!window.confirm(`确认删除策略 ${strategyId} ?`)) return;
    setBusy(true);
    try {
      await apiPost(`/strategy/${encodeURIComponent(strategyId)}/delete`);
      setMsg(`已删除策略 ${strategyId}`);
      await loadRows();
    } catch (err) {
      setError((err as Error).message || '删除失败');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="min-h-screen bg-dashboard-bg text-dashboard-text px-5 py-4 space-y-4">
      <header className="card px-4 py-3 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold m-0">策略全生命周期管理</h1>
          <div className="text-xs text-dashboard-muted mt-1">按状态分组展示，支持一键暂停、清理亏损策略、导出报告</div>
        </div>
        <div className="flex items-center gap-2">
          <a href="/dashboard" className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563]">仪表盘</a>
          <a href="/workshop" className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563]">策略工坊</a>
          <a href="/history" className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563]">历史</a>
          <a href="/settings" className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563]">设置</a>
          <button onClick={() => void loadRows()} className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563] inline-flex items-center gap-1">
            <RefreshCw size={14} />
            刷新
          </button>
        </div>
      </header>

      <section className="card p-3 flex items-center gap-2">
        <button
          onClick={() => void pauseAll()}
          disabled={busy}
          className="rounded-lg border border-yellow-500/40 bg-yellow-500/10 px-3 py-2 text-yellow-300 hover:border-yellow-400 disabled:opacity-60 inline-flex items-center gap-1"
        >
          <PauseCircle size={14} />
          全部暂停
        </button>
        <button
          onClick={() => void cleanupLosers()}
          disabled={busy}
          className="rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-rose-300 hover:border-rose-400 disabled:opacity-60 inline-flex items-center gap-1"
        >
          <Trash2 size={14} />
          清理亏损策略
        </button>
        <a
          href="/api/strategies/export.csv"
          className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563] inline-flex items-center gap-1"
        >
          <Download size={14} />
          导出报告
        </a>
        <div className="ml-auto text-xs text-dashboard-muted">最近刷新: {refreshAt || '-'}</div>
      </section>

      <section className="space-y-4">
        <div className="card p-3">
          <div className="text-sm font-semibold text-dashboard-good mb-3">🟢 运行中（按 PnL 降序）</div>
          <div className="grid grid-cols-4 gap-3">
            {grouped.running.map((row) => (
              <StrategyThumb key={row.strategy_id} row={row} onDelete={deleteStrategy} />
            ))}
          </div>
          {grouped.running.length === 0 ? <div className="text-dashboard-muted text-sm py-8 text-center">暂无运行中策略</div> : null}
        </div>

        <div className="card p-3">
          <div className="text-sm font-semibold text-yellow-300 mb-3">🟡 已暂停</div>
          <div className="grid grid-cols-4 gap-3">
            {grouped.paused.map((row) => (
              <StrategyThumb key={row.strategy_id} row={row} onDelete={deleteStrategy} />
            ))}
          </div>
          {grouped.paused.length === 0 ? <div className="text-dashboard-muted text-sm py-8 text-center">暂无暂停策略</div> : null}
        </div>

        <div className="card p-3">
          <div className="text-sm font-semibold text-dashboard-bad mb-3">🔴 已停止</div>
          <div className="grid grid-cols-4 gap-3">
            {grouped.stopped.map((row) => (
              <StrategyThumb key={row.strategy_id} row={row} onDelete={deleteStrategy} />
            ))}
          </div>
          {grouped.stopped.length === 0 ? <div className="text-dashboard-muted text-sm py-8 text-center">暂无停止策略</div> : null}
        </div>
      </section>

      {loading ? <div className="text-sm text-dashboard-muted">加载中...</div> : null}
      {msg ? <div className="text-sm text-dashboard-good">{msg}</div> : null}
      {error ? <div className="text-sm text-dashboard-bad">{error}</div> : null}
      <div className="text-xs text-dashboard-muted">时间显示: {toLocalTime(new Date().toISOString())}</div>
    </div>
  );
}
