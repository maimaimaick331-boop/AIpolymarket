import { useCallback, useEffect, useMemo, useState } from 'react';
import { Download, PauseCircle, RefreshCw, Trash2 } from 'lucide-react';
import { apiGet, apiPost, toLocalTime } from './lib/api';
import type { AccountSummary, StrategyRow } from './types';

const BUILTIN_IDS = new Set(['arb_detector', 'market_maker', 'ai_probability']);
const BUILTIN_ORDER = ['ai_probability', 'arb_detector', 'market_maker'];
const BUILTIN_META: Record<string, { label: string; desc: string }> = {
  ai_probability: {
    label: 'AI 概率评估器',
    desc: '用 LLM 估计事件概率，对比市场价格偏差，触发概率型交易信号。',
  },
  arb_detector: {
    label: '套利检测器',
    desc: '扫描 Yes+No 定价偏离，识别可套利价差并输出对冲信号。',
  },
  market_maker: {
    label: '自动做市器',
    desc: '在可交易区间双边挂单，基于 spread 与库存偏斜做再平衡。',
  },
};

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
  onStart,
  onPause,
  onDelete,
  builtinMeta,
}: {
  row: StrategyRow;
  onStart: (strategyId: string) => void;
  onPause: (strategyId: string) => void;
  onDelete: (strategyId: string, name: string) => void;
  builtinMeta?: { label: string; desc: string };
}) {
  const pnl = Number(row.total_pnl || 0);
  const winPct = Math.max(0, Math.min(100, Number(row.win_rate || 0) * 100));
  const canDelete = !BUILTIN_IDS.has(row.strategy_id);
  const todayPnl = Number((row as StrategyRow & { today_pnl?: number }).today_pnl || 0);
  const reason = row.status === 'paused' ? row.pause_reason : row.status === 'stopped' ? row.stop_reason : '';
  const statusText = row.status === 'running' ? '运行中' : row.status === 'paused' ? '已暂停' : '已停止';

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
            {builtinMeta ? <div className="text-[11px] text-sky-300 mt-0.5">系统基线: {builtinMeta.label}</div> : null}
          </div>
          <div className="inline-flex items-center gap-2">
            <span className={`inline-block h-2.5 w-2.5 rounded-full ${statusDot(row.status)}`} />
            <span className="text-xs text-dashboard-muted">{statusText}</span>
          </div>
        </div>
        <div className={`mt-2 text-2xl font-bold ${pnl >= 0 ? 'text-dashboard-good' : 'text-dashboard-bad'}`}>
          {pnl >= 0 ? '+' : ''}
          {pnl.toFixed(4)} USDC
        </div>
        {todayPnl !== 0 ? (
          <div className={`text-xs ${todayPnl >= 0 ? 'text-dashboard-good' : 'text-dashboard-bad'}`}>
            今日: {todayPnl >= 0 ? '+' : ''}
            {todayPnl.toFixed(4)}
          </div>
        ) : null}
        <div className="flex items-center gap-4 text-xs text-dashboard-muted mt-2 flex-wrap">
          <span>胜率: {(Number(row.win_rate || 0) * 100).toFixed(1)}%</span>
          <span>交易: {Number(row.trade_count || 0)}笔</span>
          <span>运行: {Number(row.runtime_hours || 0).toFixed(1)}h</span>
          <span>回撤: {Number(row.max_drawdown_pct || 0).toFixed(2)}%</span>
        </div>
        {builtinMeta ? (
          <div className="mt-2 rounded border border-sky-500/20 bg-sky-500/5 px-2 py-1 text-[11px] text-sky-200">
            {builtinMeta.desc}
          </div>
        ) : null}
        {reason ? <div className="mt-2 text-xs text-yellow-300">原因: {reason}</div> : null}
      </button>
      <div className="flex items-center gap-2 mt-3 pt-3 border-t border-dashboard-line">
        {row.status === 'running' ? (
          <button
            onClick={() => onPause(row.strategy_id)}
            className="rounded border border-yellow-500/50 bg-yellow-500/10 px-3 py-1 text-xs text-yellow-300 hover:bg-yellow-500/20"
          >
            ⏸ 暂停
          </button>
        ) : (
          <button
            onClick={() => onStart(row.strategy_id)}
            className="rounded border border-dashboard-good/50 bg-dashboard-good/10 px-3 py-1 text-xs text-dashboard-good hover:bg-dashboard-good/20"
          >
            ▶ 启动
          </button>
        )}
        <a
          href={`/strategy/${encodeURIComponent(row.strategy_id)}`}
          className="rounded border border-dashboard-line bg-[#111827] px-3 py-1 text-xs text-dashboard-muted hover:text-dashboard-text hover:border-[#4b5563]"
        >
          📊 详情
        </a>
        {canDelete ? (
          <button
            onClick={() => onDelete(row.strategy_id, row.name || row.strategy_id)}
            className="rounded border border-dashboard-bad/50 bg-dashboard-bad/10 px-3 py-1 text-xs text-dashboard-bad hover:bg-dashboard-bad/20 ml-auto"
          >
            🗑 删除
          </button>
        ) : (
          <span className="ml-auto text-xs text-dashboard-muted">内置策略不可删除</span>
        )}
      </div>
    </div>
  );
}

export default function StrategyLifecyclePage() {
  const [rows, setRows] = useState<StrategyRow[]>([]);
  const [summary, setSummary] = useState<AccountSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshAt, setRefreshAt] = useState('');
  const [msg, setMsg] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [sortBy, setSortBy] = useState<'pnl' | 'trades' | 'winrate' | 'runtime'>('pnl');

  const loadData = useCallback(async () => {
    try {
      const [out, summaryData] = await Promise.all([
        apiGet<{ rows: StrategyRow[] }>('/strategies'),
        apiGet<AccountSummary>('/account/summary'),
      ]);
      setRows(out.rows || []);
      setSummary(summaryData || null);
      setRefreshAt(new Date().toLocaleString('zh-CN', { hour12: false }));
      setError('');
    } catch (err) {
      setError((err as Error).message || '加载失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadData();
    const timer = window.setInterval(() => {
      void loadData();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [loadData]);

  const grouped = useMemo(() => {
    const score = (row: StrategyRow): number => {
      if (sortBy === 'trades') return Number(row.trade_count || 0);
      if (sortBy === 'winrate') return Number(row.win_rate || 0);
      if (sortBy === 'runtime') return Number(row.runtime_hours || 0);
      return Number(row.total_pnl || 0);
    };
    const sorter = (a: StrategyRow, b: StrategyRow) => score(b) - score(a);
    const builtinRows = rows
      .filter((x) => BUILTIN_IDS.has(x.strategy_id))
      .sort((a, b) => BUILTIN_ORDER.indexOf(a.strategy_id) - BUILTIN_ORDER.indexOf(b.strategy_id));
    const customRows = rows.filter((x) => !BUILTIN_IDS.has(x.strategy_id));
    const running = customRows.filter((x) => x.status === 'running').sort(sorter);
    const paused = customRows.filter((x) => x.status === 'paused').sort(sorter);
    const stopped = customRows.filter((x) => x.status === 'stopped').sort(sorter);
    return { builtinRows, running, paused, stopped };
  }, [rows, sortBy]);

  const initialCash = useMemo(() => {
    if (!summary) return 0;
    return Number(summary.balance_usdc || 0) - Number(summary.today_pnl || 0);
  }, [summary]);

  const totalPnl = useMemo(() => rows.reduce((sum, s) => sum + Number(s.total_pnl || 0), 0), [rows]);
  const todayPnl = useMemo(
    () =>
      rows.reduce((sum, s) => {
        const row = s as StrategyRow & { today_pnl?: number };
        return sum + Number(row.today_pnl || 0);
      }, 0),
    [rows],
  );
  const profitableCount = useMemo(() => rows.filter((s) => Number(s.total_pnl || 0) > 0).length, [rows]);

  async function pauseAll() {
    if (busy) return;
    setBusy(true);
    try {
      const out = await apiPost<ActionResp>('/strategies/pause-all');
      setMsg(`已暂停所有策略，变更 ${out.changed ?? 0} 个`);
      await loadData();
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
      await loadData();
    } catch (err) {
      setError((err as Error).message || '清理失败');
    } finally {
      setBusy(false);
    }
  }

  async function startStrategy(id: string) {
    if (busy) return;
    setBusy(true);
    try {
      await apiPost(`/strategy/${encodeURIComponent(id)}/start`);
      setMsg(`已启动策略 ${id}`);
      await loadData();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function pauseStrategy(id: string) {
    if (busy) return;
    setBusy(true);
    try {
      await apiPost(`/strategy/${encodeURIComponent(id)}/stop`);
      setMsg(`已暂停策略 ${id}`);
      await loadData();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function deleteStrategy(strategyId: string, name: string) {
    if (busy) return;
    const confirmed = window.confirm(
      `确定要删除策略「${name || strategyId}」吗？\n\n删除后该策略的交易记录将归档到历史页面，此操作不可撤销。`,
    );
    if (!confirmed) return;
    setBusy(true);
    try {
      await apiPost(`/strategy/${encodeURIComponent(strategyId)}/delete`);
      setMsg(`已删除策略 ${strategyId}`);
      await loadData();
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
          <a
            href="/live"
            className="rounded-lg border border-orange-500/50 bg-[#111827] px-3 py-2 hover:border-orange-400 inline-flex items-center gap-2 text-orange-300"
          >
            🔴 实盘中心
          </a>
          <a href="/workshop" className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563]">策略工坊</a>
          <a href="/history" className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563]">历史</a>
          <a href="/settings" className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563]">设置</a>
          <button onClick={() => void loadData()} className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563] inline-flex items-center gap-1">
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
        <select
          value={sortBy}
          onChange={(e) => setSortBy(e.target.value as 'pnl' | 'trades' | 'winrate' | 'runtime')}
          className="bg-[#111827] border border-dashboard-line rounded px-2 py-1 text-xs text-dashboard-muted"
        >
          <option value="pnl">按 PnL 排序</option>
          <option value="trades">按交易数排序</option>
          <option value="winrate">按胜率排序</option>
          <option value="runtime">按运行时长排序</option>
        </select>
        <div className="ml-auto text-xs text-dashboard-muted">最近刷新: {refreshAt || '-'}</div>
      </section>

      <section className="grid grid-cols-5 gap-3 mb-4">
        <div className="card p-3">
          <div className="text-xs text-dashboard-muted">起始资金</div>
          <div className="text-xl font-bold mt-1">{initialCash.toFixed(2)} USDC</div>
        </div>
        <div className="card p-3">
          <div className="text-xs text-dashboard-muted">当前净值</div>
          <div className={`text-xl font-bold mt-1 ${totalPnl >= 0 ? 'text-dashboard-good' : 'text-dashboard-bad'}`}>
            {(initialCash + totalPnl).toFixed(2)} USDC
          </div>
        </div>
        <div className="card p-3">
          <div className="text-xs text-dashboard-muted">累计盈亏</div>
          <div className={`text-xl font-bold mt-1 ${totalPnl >= 0 ? 'text-dashboard-good' : 'text-dashboard-bad'}`}>
            {totalPnl >= 0 ? '+' : ''}
            {totalPnl.toFixed(4)}
          </div>
        </div>
        <div className="card p-3">
          <div className="text-xs text-dashboard-muted">盈利策略 / 总策略</div>
          <div className="text-xl font-bold mt-1">
            <span className="text-dashboard-good">{profitableCount}</span>
            <span className="text-dashboard-muted"> / </span>
            <span>{rows.length}</span>
          </div>
        </div>
        <div className="card p-3">
          <div className="text-xs text-dashboard-muted">今日盈亏</div>
          <div className={`text-xl font-bold mt-1 ${todayPnl >= 0 ? 'text-dashboard-good' : 'text-dashboard-bad'}`}>
            {todayPnl >= 0 ? '+' : ''}
            {todayPnl.toFixed(4)}
          </div>
        </div>
      </section>

      <section className="space-y-4">
        <div className="card p-3">
          <div className="text-sm font-semibold text-sky-300 mb-1">🧩 系统内置基线策略（独立分组）</div>
          <div className="text-xs text-dashboard-muted mb-3">
            这 3 个策略用于基线对照、风控联动与系统可用性验证。建议仅做启动/暂停，不要误删或误改。
          </div>
          <div className="grid grid-cols-4 gap-3">
            {grouped.builtinRows.map((row) => (
              <StrategyThumb
                key={row.strategy_id}
                row={row}
                onStart={startStrategy}
                onPause={pauseStrategy}
                onDelete={deleteStrategy}
                builtinMeta={BUILTIN_META[row.strategy_id]}
              />
            ))}
          </div>
          {grouped.builtinRows.length === 0 ? <div className="text-dashboard-muted text-sm py-6 text-center">未检测到内置策略</div> : null}
        </div>

        <div className="card p-3">
          <div className="text-sm font-semibold text-dashboard-good mb-3">🟢 用户策略运行中</div>
          <div className="grid grid-cols-4 gap-3">
            {grouped.running.map((row) => (
              <StrategyThumb key={row.strategy_id} row={row} onStart={startStrategy} onPause={pauseStrategy} onDelete={deleteStrategy} />
            ))}
          </div>
          {grouped.running.length === 0 ? <div className="text-dashboard-muted text-sm py-8 text-center">暂无运行中策略</div> : null}
        </div>

        <div className="card p-3">
          <div className="text-sm font-semibold text-yellow-300 mb-3">🟡 用户策略已暂停</div>
          <div className="grid grid-cols-4 gap-3">
            {grouped.paused.map((row) => (
              <StrategyThumb key={row.strategy_id} row={row} onStart={startStrategy} onPause={pauseStrategy} onDelete={deleteStrategy} />
            ))}
          </div>
          {grouped.paused.length === 0 ? <div className="text-dashboard-muted text-sm py-8 text-center">暂无暂停策略</div> : null}
        </div>

        <div className="card p-3">
          <div className="text-sm font-semibold text-dashboard-bad mb-3">🔴 用户策略已停止</div>
          <div className="grid grid-cols-4 gap-3">
            {grouped.stopped.map((row) => (
              <StrategyThumb key={row.strategy_id} row={row} onStart={startStrategy} onPause={pauseStrategy} onDelete={deleteStrategy} />
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
