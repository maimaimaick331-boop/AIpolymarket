import { useCallback, useEffect, useMemo, useState } from 'react';
import { ArrowLeft, Download, Pause, Play, RefreshCw, Trash2 } from 'lucide-react';
import { apiGet, apiPost, toLocalTime } from './lib/api';
import PnlAreaChart from './components/PnlAreaChart';
import type { StrategyOverviewResponse, StrategyParamHistoryRow, StrategyRow, StrategyTradeRow } from './types';

interface DeletePreview {
  strategy_id: string;
  name: string;
  total_pnl: number;
  trade_count: number;
  runtime_days: number;
  status: string;
}

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

function valueToText(v: unknown): string {
  if (typeof v === 'string') return v;
  return JSON.stringify(v);
}

function parseInputValue(raw: string): unknown {
  const s = raw.trim();
  if (s === '') return '';
  if (s === 'true') return true;
  if (s === 'false') return false;
  const n = Number(s);
  if (!Number.isNaN(n) && s.match(/^[-+]?\d+(\.\d+)?$/)) return n;
  if ((s.startsWith('{') && s.endsWith('}')) || (s.startsWith('[') && s.endsWith(']'))) {
    try {
      return JSON.parse(s);
    } catch {
      return raw;
    }
  }
  return raw;
}

function tradeRowClass(row: StrategyTradeRow): string {
  const pnl = Number(row.pnl || 0);
  if (pnl > 0) return 'bg-emerald-400/10';
  if (pnl < 0) return 'bg-rose-400/10';
  return '';
}

function normalizedDecision(decision: string): 'buy' | 'sell' | 'hold' {
  const text = String(decision || '').trim().toLowerCase();
  if (text.includes('buy') || text.includes('买')) return 'buy';
  if (text.includes('sell') || text.includes('卖')) return 'sell';
  return 'hold';
}

function displayMarketName(zh?: string, en?: string, fallback?: string): string {
  const zhText = String(zh || '').trim();
  const enText = String(en || '').trim();
  if (zhText && (zhText !== enText || /[\u4e00-\u9fff]/.test(zhText))) return zhText;
  return zhText || enText || String(fallback || '').trim() || '-';
}

export default function StrategyDetailPage() {
  const strategyId = useMemo(() => decodeURIComponent(window.location.pathname.split('/').filter(Boolean).pop() || ''), []);
  const [overview, setOverview] = useState<StrategyOverviewResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [paramDraft, setParamDraft] = useState<Record<string, string>>({});
  const [newParamKey, setNewParamKey] = useState('');
  const [newParamValue, setNewParamValue] = useState('');
  const [saveNote, setSaveNote] = useState('');
  const [deletePreview, setDeletePreview] = useState<DeletePreview | null>(null);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [insightSignalFilter, setInsightSignalFilter] = useState('all');
  const [insightDecisionFilter, setInsightDecisionFilter] = useState<'all' | 'buy' | 'sell' | 'hold'>('all');
  const [insightSearch, setInsightSearch] = useState('');
  const [insightTriggeredOnly, setInsightTriggeredOnly] = useState(false);

  const loadData = useCallback(async () => {
    if (!strategyId) return;
    try {
      const sid = encodeURIComponent(strategyId);
      const [ov, listResp] = await Promise.all([
        apiGet<StrategyOverviewResponse>(`/strategy/${sid}/overview?insight_limit=40&pnl_limit=5000`),
        apiGet<{ rows: StrategyRow[] }>('/strategies').catch(() => ({ rows: [] })),
      ]);

      const listRows = Array.isArray(listResp?.rows) ? listResp.rows : [];
      const listRow = listRows.find((x) => String(x.strategy_id || '') === strategyId);

      const merged: StrategyOverviewResponse = {
        ...ov,
        strategy: (listRow ? { ...ov.strategy, ...listRow } : ov.strategy) as StrategyRow,
        metrics: {
          ...(ov.metrics || {}),
          trade_count:
            Number(ov?.metrics?.trade_count || 0) > 0
              ? Number(ov.metrics?.trade_count || 0)
              : Number(listRow?.trade_count || ov?.metrics?.trade_count || 0),
          runtime_hours:
            Number(ov?.metrics?.runtime_hours || 0) > 0
              ? Number(ov.metrics?.runtime_hours || 0)
              : Number(listRow?.runtime_hours || ov?.metrics?.runtime_hours || 0),
          win_rate:
            Number(ov?.metrics?.win_rate || 0) > 0
              ? Number(ov.metrics?.win_rate || 0)
              : Number(listRow?.win_rate || ov?.metrics?.win_rate || 0),
          total_pnl:
            Math.abs(Number(ov?.metrics?.total_pnl || 0)) > 1e-12
              ? Number(ov.metrics?.total_pnl || 0)
              : Number(listRow?.total_pnl || ov?.metrics?.total_pnl || 0),
          max_drawdown_pct:
            Number(ov?.metrics?.max_drawdown_pct || 0) > 0
              ? Number(ov.metrics?.max_drawdown_pct || 0)
              : Number(listRow?.max_drawdown_pct || ov?.metrics?.max_drawdown_pct || 0),
        },
      };

      if (!Array.isArray(merged.trades) || merged.trades.length === 0) {
        try {
          const tr = await apiGet<{ rows?: StrategyTradeRow[] }>(`/strategy/${sid}/trades?limit=200`);
          if (Array.isArray(tr?.rows) && tr.rows.length > 0) {
            merged.trades = tr.rows;
          }
        } catch {
          // keep overview payload
        }
      }

      if (!Array.isArray(merged.insights) || merged.insights.length === 0) {
        try {
          const ins = await apiGet<{ rows?: StrategyOverviewResponse['insights'] }>(`/strategy/${sid}/insights?limit=40`);
          if (Array.isArray(ins?.rows) && ins.rows.length > 0) {
            merged.insights = ins.rows;
          }
        } catch {
          // keep overview payload
        }
      }

      setOverview(merged);
      setError('');
    } catch (err) {
      setError((err as Error).message || '加载失败');
    } finally {
      setLoading(false);
    }
  }, [strategyId]);

  useEffect(() => {
    void loadData();
    const timer = window.setInterval(() => {
      void loadData();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [loadData]);

  useEffect(() => {
    const params = (overview?.strategy?.params || {}) as Record<string, unknown>;
    const draft: Record<string, string> = {};
    for (const [k, v] of Object.entries(params)) {
      draft[k] = valueToText(v);
    }
    setParamDraft(draft);
  }, [overview?.strategy?.strategy_id, overview?.strategy?.params]);

  async function toggleStatus() {
    const row = overview?.strategy as StrategyRow | undefined;
    if (!row || busy) return;
    setBusy(true);
    try {
      if (row.status === 'running') {
        await apiPost(`/strategy/${encodeURIComponent(row.strategy_id)}/stop`);
      } else {
        await apiPost(`/strategy/${encodeURIComponent(row.strategy_id)}/start`);
      }
      await loadData();
    } catch (err) {
      setError((err as Error).message || '操作失败');
    } finally {
      setBusy(false);
    }
  }

  async function saveParams() {
    if (busy) return;
    setBusy(true);
    try {
      const params: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(paramDraft)) {
        params[k] = parseInputValue(v);
      }
      if (newParamKey.trim()) {
        params[newParamKey.trim()] = parseInputValue(newParamValue);
      }
      await apiPost(`/strategy/${encodeURIComponent(strategyId)}/params`, {
        params,
        note: saveNote || 'inline_edit',
      });
      setNewParamKey('');
      setNewParamValue('');
      setSaveNote('');
      await loadData();
    } catch (err) {
      setError((err as Error).message || '参数保存失败');
    } finally {
      setBusy(false);
    }
  }

  async function openDeleteConfirm() {
    setBusy(true);
    try {
      const p = await apiGet<DeletePreview>(`/strategy/${encodeURIComponent(strategyId)}/delete-preview`);
      setDeletePreview(p);
      setDeleteOpen(true);
      setError('');
    } catch (err) {
      setError((err as Error).message || '读取删除预览失败');
    } finally {
      setBusy(false);
    }
  }

  async function confirmDelete() {
    if (busy) return;
    setBusy(true);
    try {
      await apiPost(`/strategy/${encodeURIComponent(strategyId)}/delete`);
      window.location.href = '/strategies';
    } catch (err) {
      setError((err as Error).message || '删除失败');
      setBusy(false);
    }
  }

  function toggleExpanded(key: string) {
    setExpanded((prev) => ({ ...prev, [key]: !prev[key] }));
  }

  function historySummary(row: StrategyParamHistoryRow): string {
    const changes = row.change || {};
    const keys = Object.keys(changes);
    if (keys.length === 0) return '-';
    return keys.slice(0, 3).join(', ') + (keys.length > 3 ? ` +${keys.length - 3}` : '');
  }

  const s = overview?.strategy as StrategyRow | undefined;
  const m = overview?.metrics;
  const points = overview?.pnl_rows || [];
  const trades = (overview?.trades || []).slice(0, 80);
  const insights = overview?.insights || [];
  const paramHistory = overview?.param_history || [];
  const reasonText = s?.status === 'paused' ? s.pause_reason : s?.status === 'stopped' ? s.stop_reason : '';
  const isBuiltin = s ? ['arb_detector', 'market_maker', 'ai_probability'].includes(s.strategy_id) : false;
  const insightSignalOptions = useMemo(
    () =>
      Array.from(
        new Set(
          insights
            .map((x) => String(x.signal_type || '').trim())
            .filter(Boolean),
        ),
      ).sort(),
    [insights],
  );
  const insightStats = useMemo(() => {
    const out = { total: insights.length, triggered: 0, buy: 0, sell: 0, hold: 0 };
    for (const row of insights) {
      if (row.triggered) out.triggered += 1;
      const d = normalizedDecision(String(row.decision || ''));
      out[d] += 1;
    }
    return out;
  }, [insights]);
  const visibleInsights = useMemo(() => {
    const key = insightSearch.trim().toLowerCase();
    return insights.filter((row) => {
      const signalType = String(row.signal_type || '').trim();
      if (insightSignalFilter !== 'all' && signalType !== insightSignalFilter) return false;
      const decision = normalizedDecision(String(row.decision || ''));
      if (insightDecisionFilter !== 'all' && decision !== insightDecisionFilter) return false;
      if (insightTriggeredOnly && !row.triggered) return false;
      if (!key) return true;
      const blob = `${row.market_name || ''} ${row.market_name_en || ''} ${row.source_title || ''} ${row.decision_reason || ''}`.toLowerCase();
      return blob.includes(key);
    });
  }, [insights, insightSignalFilter, insightDecisionFilter, insightTriggeredOnly, insightSearch]);

  return (
    <div className="min-h-screen bg-dashboard-bg text-dashboard-text px-5 py-4 space-y-4">
      <header className="card px-4 py-3 flex items-center justify-between">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold m-0 truncate">{s?.name || strategyId}</h1>
          <div className="text-xs text-dashboard-muted mt-1 font-mono">
            {strategyId} | 运行时长 {formatRuntime(Number(m?.runtime_hours || s?.runtime_hours || 0))}
          </div>
          {reasonText ? <div className="text-xs text-yellow-300 mt-1">原因: {reasonText}</div> : null}
        </div>
        <div className="flex items-center gap-2">
          <span className={`inline-block h-2.5 w-2.5 rounded-full ${statusDot(s?.status || 'stopped')}`} />
          <span className="text-sm">{s?.status || 'stopped'}</span>
          <a href="/strategies" className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563] inline-flex items-center gap-1">
            <ArrowLeft size={14} />
            返回
          </a>
          <a href="/workshop" className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563]">工坊</a>
          <a href="/history" className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563]">历史</a>
          <a href="/settings" className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563]">设置</a>
          <button
            onClick={() => void loadData()}
            className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563] inline-flex items-center gap-1"
          >
            <RefreshCw size={14} />
            刷新
          </button>
          <button
            onClick={() => void toggleStatus()}
            disabled={busy}
            className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563] disabled:opacity-60 inline-flex items-center gap-1"
          >
            {s?.status === 'running' ? <Pause size={14} /> : <Play size={14} />}
            {s?.status === 'running' ? '暂停' : '恢复'}
          </button>
          <a
            href={`/api/strategy/${encodeURIComponent(strategyId)}/export.csv`}
            className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563] inline-flex items-center gap-1"
          >
            <Download size={14} />
            导出
          </a>
          <button
            onClick={() => void openDeleteConfirm()}
            disabled={busy}
            className="rounded-lg border border-rose-500/50 bg-rose-500/10 px-3 py-2 text-rose-300 hover:border-rose-400 disabled:opacity-60 inline-flex items-center gap-1"
          >
            <Trash2 size={14} />
            删除
          </button>
        </div>
      </header>

      <section className="card p-3">
        <div className="text-sm text-dashboard-muted mb-2">PnL 累计曲线</div>
        <PnlAreaChart points={points} />
      </section>

      <section className="grid grid-cols-6 gap-3">
        <article className="card p-3">
          <div className="text-xs text-dashboard-muted">总PnL</div>
          <div className={`mt-1 text-lg font-semibold ${Number(m?.total_pnl || 0) >= 0 ? 'text-dashboard-good' : 'text-dashboard-bad'}`}>
            {Number(m?.total_pnl || 0).toFixed(4)}
          </div>
        </article>
        <article className="card p-3">
          <div className="text-xs text-dashboard-muted">今日PnL</div>
          <div className={`mt-1 text-lg font-semibold ${Number(m?.today_pnl || 0) >= 0 ? 'text-dashboard-good' : 'text-dashboard-bad'}`}>
            {Number(m?.today_pnl || 0).toFixed(4)}
          </div>
        </article>
        <article className="card p-3">
          <div className="text-xs text-dashboard-muted">胜率</div>
          <div className="mt-1 text-lg font-semibold">{(Number(m?.win_rate || 0) * 100).toFixed(2)}%</div>
        </article>
        <article className="card p-3">
          <div className="text-xs text-dashboard-muted">盈亏比</div>
          <div className="mt-1 text-lg font-semibold">{Number(m?.profit_factor || 0).toFixed(3)}</div>
        </article>
        <article className="card p-3">
          <div className="text-xs text-dashboard-muted">最大回撤</div>
          <div className="mt-1 text-lg font-semibold">{Number(m?.max_drawdown_pct || 0).toFixed(3)}%</div>
        </article>
        <article className="card p-3">
          <div className="text-xs text-dashboard-muted">交易笔数</div>
          <div className="mt-1 text-lg font-semibold">{Number(m?.trade_count || 0)}</div>
        </article>
      </section>

      <section className="card p-3 space-y-3">
        <div className="flex items-center justify-between gap-3">
          <div className="text-sm text-dashboard-muted">信息面板（时间线）</div>
          <div className="text-xs text-dashboard-muted">筛选后 {visibleInsights.length} / 总 {insightStats.total}</div>
        </div>
        <div className="grid grid-cols-5 gap-2">
          <div className="rounded-lg border border-dashboard-line bg-[#111827] px-2 py-1.5 text-xs">总信号: {insightStats.total}</div>
          <div className="rounded-lg border border-dashboard-line bg-[#111827] px-2 py-1.5 text-xs text-dashboard-good">已触发: {insightStats.triggered}</div>
          <div className="rounded-lg border border-dashboard-line bg-[#111827] px-2 py-1.5 text-xs">买入: {insightStats.buy}</div>
          <div className="rounded-lg border border-dashboard-line bg-[#111827] px-2 py-1.5 text-xs">卖出: {insightStats.sell}</div>
          <div className="rounded-lg border border-dashboard-line bg-[#111827] px-2 py-1.5 text-xs">不操作: {insightStats.hold}</div>
        </div>
        <div className="grid grid-cols-12 gap-2">
          <select
            value={insightSignalFilter}
            onChange={(e) => setInsightSignalFilter(e.target.value)}
            className="col-span-3 rounded-lg border border-dashboard-line bg-[#111827] px-2 py-2 text-xs"
          >
            <option value="all">全部信号类型</option>
            {insightSignalOptions.map((x) => (
              <option key={x} value={x}>{x}</option>
            ))}
          </select>
          <select
            value={insightDecisionFilter}
            onChange={(e) => setInsightDecisionFilter(e.target.value as 'all' | 'buy' | 'sell' | 'hold')}
            className="col-span-2 rounded-lg border border-dashboard-line bg-[#111827] px-2 py-2 text-xs"
          >
            <option value="all">全部决策</option>
            <option value="buy">买入</option>
            <option value="sell">卖出</option>
            <option value="hold">不操作</option>
          </select>
          <label className="col-span-2 rounded-lg border border-dashboard-line bg-[#111827] px-2 py-2 text-xs inline-flex items-center gap-2">
            <input
              type="checkbox"
              checked={insightTriggeredOnly}
              onChange={(e) => setInsightTriggeredOnly(e.target.checked)}
            />
            仅看触发
          </label>
          <input
            value={insightSearch}
            onChange={(e) => setInsightSearch(e.target.value)}
            placeholder="搜索市场/来源/原因"
            className="col-span-5 rounded-lg border border-dashboard-line bg-[#111827] px-2 py-2 text-xs"
          />
        </div>
        <div className="space-y-2 max-h-[320px] overflow-auto scroll-dark pr-1">
          {visibleInsights.map((row, idx) => {
            const key = `${row.time_utc}-${row.market_id}-${idx}`;
            const isOpen = expanded[key] ?? idx < 4;
            const deviationPct = Number(row.deviation || 0) * 100;
            const confPct = Number(row.confidence || 0) * 100;
            const marketTitle = displayMarketName(row.market_name, row.market_name_en, row.source_title);
            return (
              <article key={key} className="rounded-lg border border-dashboard-line bg-[#111827] text-sm">
                <button
                  onClick={() => toggleExpanded(key)}
                  className="w-full text-left p-3 hover:bg-white/[0.02] transition-colors"
                >
                  <div className="flex items-center justify-between gap-2">
                    <div className="min-w-0">
                      <div className="text-dashboard-text truncate">
                        🕐 {toLocalTime(row.time_utc)} | {row.signal_type || 'signal'} | 📰 {marketTitle}
                      </div>
                      <div className="text-xs text-dashboard-muted mt-1">
                        AI {(Number(row.ai_probability || 0) * 100).toFixed(1)}% | 市场 {Number(row.market_yes_price || 0).toFixed(3)} | 偏差 {deviationPct.toFixed(2)}% | 置信度 {confPct.toFixed(1)}%
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      <span className={`text-xs rounded-full px-2 py-0.5 ${row.triggered ? 'bg-emerald-400/15 text-dashboard-good' : 'bg-yellow-400/15 text-yellow-300'}`}>
                        {row.triggered ? '已触发' : '未触发'}
                      </span>
                      <span className="text-xs text-dashboard-muted">{isOpen ? '收起' : '展开'}</span>
                    </div>
                  </div>
                </button>

                {isOpen ? (
                  <div className="px-3 pb-3 space-y-1 border-t border-dashboard-line">
                    <div className="text-dashboard-muted text-xs mt-2">
                      来源: {row.source_name || 'Unknown'}
                      {row.source_url ? (
                        <>
                          {' '}
                          | <a className="text-sky-300 hover:underline" href={row.source_url} target="_blank" rel="noreferrer">链接</a>
                        </>
                      ) : null}
                    </div>
                    <div className="text-xs">AI评估: {(Number(row.ai_probability || 0) * 100).toFixed(1)}% / conf {confPct.toFixed(1)}%</div>
                    <div className="text-xs">市场价格: Yes = {Number(row.market_yes_price || 0).toFixed(4)}</div>
                    <div className="text-xs">偏差: {deviationPct >= 0 ? '+' : ''}{deviationPct.toFixed(2)}%</div>
                    <div className="text-xs">最终决策: {row.decision} | 原因: {row.decision_reason || '-'}</div>
                    {row.execution ? <div className="text-xs text-dashboard-good">执行: {row.execution}</div> : null}
                  </div>
                ) : null}
              </article>
            );
          })}
          {visibleInsights.length === 0 ? <div className="text-dashboard-muted text-sm py-8 text-center">暂无符合筛选条件的信息面板数据</div> : null}
        </div>
      </section>

      <section className="card p-3">
        <div className="text-sm text-dashboard-muted mb-2">交易记录（时间 | 方向 | 市场 | 价格 | 数量(USDC) | 盈亏 | 决策理由）</div>
        <div className="overflow-auto scroll-dark max-h-[300px]">
          <table className="w-full text-sm min-w-[1200px]">
            <thead className="text-dashboard-muted bg-[#111827]">
              <tr>
                <th className="text-left px-2 py-2 font-medium">时间</th>
                <th className="text-left px-2 py-2 font-medium">方向</th>
                <th className="text-left px-2 py-2 font-medium">市场</th>
                <th className="text-left px-2 py-2 font-medium">价格</th>
                <th className="text-left px-2 py-2 font-medium">数量(USDC)</th>
                <th className="text-left px-2 py-2 font-medium">盈亏</th>
                <th className="text-left px-2 py-2 font-medium">决策理由</th>
              </tr>
            </thead>
            <tbody>
              {trades.map((row) => (
                <tr key={row.id} className={`border-t border-dashboard-line ${tradeRowClass(row)}`}>
                  <td className="px-2 py-2 whitespace-nowrap">{toLocalTime(row.time_utc)}</td>
                  <td className="px-2 py-2">{String(row.side || '').toUpperCase()}</td>
                  <td className="px-2 py-2 max-w-[320px] truncate" title={row.market_en || row.market}>{row.market}</td>
                  <td className="px-2 py-2">{Number(row.price || 0).toFixed(4)}</td>
                  <td className="px-2 py-2">{Number(row.cost_usdc || 0).toFixed(4)}</td>
                  <td className={`px-2 py-2 ${Number(row.pnl || 0) >= 0 ? 'text-dashboard-good' : 'text-dashboard-bad'}`}>{Number(row.pnl || 0).toFixed(4)}</td>
                  <td className="px-2 py-2 max-w-[480px] truncate" title={row.decision_reason || ''}>{row.decision_reason || '-'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {trades.length === 0 ? <div className="text-dashboard-muted text-sm py-8 text-center">暂无成交数据</div> : null}
        </div>
      </section>

      <section className="grid grid-cols-3 gap-3">
        <div className="col-span-2 card p-3 space-y-3">
          <div className="text-sm text-dashboard-muted">参数面板（inline 修改，保存后立即生效）</div>
          {isBuiltin ? <div className="text-xs text-yellow-300">当前是内置量化策略，修改项会映射到量化参数。</div> : null}
          <div className="space-y-2 max-h-[300px] overflow-auto scroll-dark pr-1">
            {Object.entries(paramDraft).map(([k, v]) => (
              <div key={k} className="grid grid-cols-12 gap-2">
                <div className="col-span-4 rounded-lg border border-dashboard-line bg-[#111827] px-2 py-2 text-xs font-mono">{k}</div>
                <input
                  value={v}
                  onChange={(e) => setParamDraft((prev) => ({ ...prev, [k]: e.target.value }))}
                  className="col-span-8 rounded-lg border border-dashboard-line bg-[#111827] px-2 py-2 text-xs"
                />
              </div>
            ))}
          </div>
          <div className="grid grid-cols-12 gap-2">
            <input value={newParamKey} onChange={(e) => setNewParamKey(e.target.value)} placeholder="新增参数 key" className="col-span-3 rounded-lg border border-dashboard-line bg-[#111827] px-2 py-2 text-xs" />
            <input value={newParamValue} onChange={(e) => setNewParamValue(e.target.value)} placeholder="新增参数 value" className="col-span-5 rounded-lg border border-dashboard-line bg-[#111827] px-2 py-2 text-xs" />
            <input value={saveNote} onChange={(e) => setSaveNote(e.target.value)} placeholder="修改备注(可选)" className="col-span-2 rounded-lg border border-dashboard-line bg-[#111827] px-2 py-2 text-xs" />
            <button
              onClick={() => void saveParams()}
              disabled={busy}
              className="col-span-2 rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563] disabled:opacity-60"
            >
              保存生效
            </button>
          </div>
        </div>

        <div className="col-span-1 card p-3">
          <div className="text-sm text-dashboard-muted mb-2">参数修改历史</div>
          <div className="space-y-2 max-h-[360px] overflow-auto scroll-dark pr-1">
            {paramHistory.map((row) => (
              <article key={row.id} className="rounded-lg border border-dashboard-line bg-[#111827] p-2 text-xs">
                <div>{toLocalTime(row.changed_at)}</div>
                <div className="text-dashboard-muted mt-1">by {row.changed_by} | {row.note || '-'}</div>
                <div className="mt-1 font-mono">{historySummary(row)}</div>
              </article>
            ))}
            {paramHistory.length === 0 ? <div className="text-dashboard-muted text-xs py-8 text-center">暂无历史</div> : null}
          </div>
        </div>
      </section>

      {deleteOpen ? (
        <div className="fixed inset-0 z-50 bg-black/65 flex items-center justify-center p-6">
          <div className="w-[520px] card p-4 space-y-3">
            <div className="text-lg font-semibold text-rose-300">确认删除策略</div>
            <div className="text-sm text-dashboard-muted">删除后会：停止策略、关闭未成交订单、从策略列表移除；成交记录会归档保留到历史页。</div>
            <div className="grid grid-cols-3 gap-2 text-sm">
              <div className="card p-2"><div className="text-xs text-dashboard-muted">总PnL</div><div className={Number(deletePreview?.total_pnl || 0) >= 0 ? 'text-dashboard-good' : 'text-dashboard-bad'}>{Number(deletePreview?.total_pnl || 0).toFixed(4)}</div></div>
              <div className="card p-2"><div className="text-xs text-dashboard-muted">运行天数</div><div>{Number(deletePreview?.runtime_days || 0).toFixed(2)}</div></div>
              <div className="card p-2"><div className="text-xs text-dashboard-muted">交易笔数</div><div>{Number(deletePreview?.trade_count || 0)}</div></div>
            </div>
            <div className="flex items-center justify-end gap-2">
              <button onClick={() => setDeleteOpen(false)} className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563]">取消</button>
              <button onClick={() => void confirmDelete()} disabled={busy} className="rounded-lg border border-rose-500/50 bg-rose-500/10 px-3 py-2 text-rose-300 hover:border-rose-400 disabled:opacity-60">确认删除</button>
            </div>
          </div>
        </div>
      ) : null}

      {loading ? <div className="text-sm text-dashboard-muted">加载中...</div> : null}
      {error ? <div className="text-sm text-dashboard-bad">{error}</div> : null}
    </div>
  );
}
