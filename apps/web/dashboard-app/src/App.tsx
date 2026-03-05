import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  Bell,
  ChevronDown,
  CircleDollarSign,
  Pause,
  Play,
  Settings,
  ShieldAlert,
  TrendingDown,
  TrendingUp,
} from 'lucide-react';
import { apiGet, apiPost, toLocalTime } from './lib/api';
import type {
  AccountSummary,
  AiEvalRow,
  LlmHealth,
  MarketMonitorRow,
  PnlHistoryResponse,
  ProviderPoolState,
  RecentTrade,
  StrategyRow,
} from './types';
import AnimatedNumber from './components/AnimatedNumber';
import PnlAreaChart from './components/PnlAreaChart';
import StrategyBarChart from './components/StrategyBarChart';
import StrategyCard from './components/StrategyCard';
import SettingsModal from './components/SettingsModal';

function riskStyle(status: string) {
  if (status === 'danger') return { text: '危险', cls: 'text-dashboard-bad', icon: <AlertTriangle size={16} /> };
  if (status === 'warning') return { text: '警告', cls: 'text-yellow-300', icon: <ShieldAlert size={16} /> };
  return { text: '正常', cls: 'text-dashboard-good', icon: <ShieldAlert size={16} /> };
}

interface AppProps {
  initialSettingsOpen?: boolean;
}

export default function App({ initialSettingsOpen = false }: AppProps) {
  const [summary, setSummary] = useState<AccountSummary | null>(null);
  const [strategies, setStrategies] = useState<StrategyRow[]>([]);
  const [pnl, setPnl] = useState<PnlHistoryResponse | null>(null);
  const [trades, setTrades] = useState<RecentTrade[]>([]);
  const [markets, setMarkets] = useState<MarketMonitorRow[]>([]);
  const [aiRows, setAiRows] = useState<AiEvalRow[]>([]);
  const [llmHealth, setLlmHealth] = useState<LlmHealth | null>(null);
  const [providerPool, setProviderPool] = useState<ProviderPoolState | null>(null);
  const [recheckingProvider, setRecheckingProvider] = useState(false);
  const [loading, setLoading] = useState(true);
  const [refreshAt, setRefreshAt] = useState('');
  const [error, setError] = useState('');
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [detail, setDetail] = useState<StrategyRow | null>(null);

  const loadData = useCallback(async () => {
    try {
      const [s, st, ph, tr, mk, ai] = await Promise.all([
        apiGet<AccountSummary>('/account/summary'),
        apiGet<{ rows: StrategyRow[] }>('/strategies'),
        apiGet<PnlHistoryResponse>('/pnl/history'),
        apiGet<{ rows: RecentTrade[] }>('/trades/recent?limit=20'),
        apiGet<{ rows: MarketMonitorRow[] }>('/markets/monitor?limit=40'),
        apiGet<{ rows: AiEvalRow[]; llm_health?: LlmHealth; provider_pool?: ProviderPoolState }>('/ai/evals?limit=200'),
      ]);
      setSummary(s);
      setStrategies(st.rows || []);
      setPnl(ph);
      setTrades(tr.rows || []);
      setMarkets(mk.rows || []);
      setAiRows(ai.rows || []);
      setLlmHealth(ai.llm_health || null);
      setProviderPool(ai.provider_pool || ai.llm_health?.provider_pool || null);
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

  useEffect(() => {
    if (initialSettingsOpen) {
      setSettingsOpen(true);
    }
  }, [initialSettingsOpen]);

  const top = summary || {
    balance_usdc: 0,
    today_pnl: 0,
    active_strategies: 0,
    total_strategies: 0,
    risk_status: 'normal',
    alerts_today: 0,
    updated_at_utc: '',
  };

  const cards = useMemo(
    () => [
      {
        key: 'balance',
        title: '账户余额 (USDC)',
        value: <AnimatedNumber value={Number(top.balance_usdc) || 0} digits={4} />,
        icon: <CircleDollarSign size={18} className="text-sky-300" />,
      },
      {
        key: 'pnl',
        title: '今日 PnL',
        value: (
          <span className={Number(top.today_pnl) >= 0 ? 'text-dashboard-good' : 'text-dashboard-bad'}>
            <AnimatedNumber value={Number(top.today_pnl) || 0} digits={4} />
          </span>
        ),
        icon: Number(top.today_pnl) >= 0 ? <TrendingUp size={18} className="text-dashboard-good" /> : <TrendingDown size={18} className="text-dashboard-bad" />,
      },
      {
        key: 'strategies',
        title: '活跃策略 / 总策略',
        value: (
          <span>
            <AnimatedNumber value={Number(top.active_strategies) || 0} digits={0} />
            <span className="text-dashboard-muted"> / </span>
            <AnimatedNumber value={Number(top.total_strategies) || 0} digits={0} />
          </span>
        ),
        icon: <Play size={18} className="text-indigo-300" />,
      },
      {
        key: 'risk',
        title: '风控状态',
        value: <span className={riskStyle(top.risk_status).cls}>{riskStyle(top.risk_status).text}</span>,
        icon: riskStyle(top.risk_status).icon,
      },
      {
        key: 'alerts',
        title: '今日警报',
        value: <AnimatedNumber value={Number(top.alerts_today) || 0} digits={0} />,
        icon: <Bell size={18} className={Number(top.alerts_today) > 0 ? 'text-yellow-300' : 'text-dashboard-muted'} />,
      },
    ],
    [top],
  );

  async function toggleStrategy(row: StrategyRow) {
    try {
      if (row.status === 'running') {
        await apiPost(`/strategy/${encodeURIComponent(row.strategy_id)}/stop`);
      } else {
        await apiPost(`/strategy/${encodeURIComponent(row.strategy_id)}/start`);
      }
      await loadData();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function recheckProviders() {
    try {
      setRecheckingProvider(true);
      const out = await apiPost<{ ok: boolean; provider_pool?: ProviderPoolState; llm_health?: LlmHealth }>('/llm/providers/recheck');
      if (out.provider_pool) setProviderPool(out.provider_pool);
      if (out.llm_health) setLlmHealth(out.llm_health);
      setRefreshAt(new Date().toLocaleString('zh-CN', { hour12: false }));
      setError('');
    } catch (err) {
      setError(`Provider 重新检测失败: ${(err as Error).message || 'unknown'}`);
    } finally {
      setRecheckingProvider(false);
    }
  }

  const currentProvider = useMemo(
    () => String(llmHealth?.provider_id || providerPool?.current_provider_id || '').trim(),
    [llmHealth, providerPool],
  );

  const providerErrors = useMemo(() => {
    const rows = llmHealth?.provider_errors;
    if (Array.isArray(rows) && rows.length > 0) {
      return rows.map((x) => ({ provider_id: String(x.provider_id || ''), error: String(x.error || 'unavailable') }));
    }
    const poolRows = providerPool?.rows || [];
    return poolRows
      .filter((x) => !x.available)
      .map((x) => ({ provider_id: String(x.provider_id || ''), error: String(x.error || x.status || 'unavailable') }));
  }, [llmHealth, providerPool]);

  const allProviderUnavailable = useMemo(() => {
    const rows = providerPool?.rows || [];
    if (!rows.length) return false;
    return rows.every((x) => !x.available);
  }, [providerPool]);

  return (
    <div className="min-h-screen bg-dashboard-bg text-dashboard-text px-5 py-4 space-y-4">
      <header className="card px-4 py-3 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold m-0">Polymarket 量化交易仪表盘</h1>
          <div className="text-xs text-dashboard-muted mt-1">监控 + 操作（配置项已收纳到右上角设置）</div>
        </div>
        <div className="flex items-center gap-3">
          <div className="text-xs text-dashboard-muted">最近刷新: {refreshAt || '-'}</div>
          <a
            href="/strategies"
            className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563] inline-flex items-center gap-2"
          >
            策略管理
          </a>
          <a
            href="/workshop"
            className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563] inline-flex items-center gap-2"
          >
            策略工坊
          </a>
          <a
            href="/history"
            className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563] inline-flex items-center gap-2"
          >
            历史
          </a>
          <a
            href="/settings"
            className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563] inline-flex items-center gap-2"
          >
            设置页
          </a>
          <button
            onClick={() => setSettingsOpen(true)}
            className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563] inline-flex items-center gap-2"
          >
            <Settings size={16} />
            设置
          </button>
        </div>
      </header>

      <section className="grid grid-cols-5 gap-3">
        {cards.map((c) => (
          <article key={c.key} className="card p-3">
            <div className="flex items-center justify-between text-dashboard-muted text-xs">
              <span>{c.title}</span>
              {c.icon}
            </div>
            <div className="mt-2 text-2xl font-bold tracking-tight transition-all duration-500">{c.value}</div>
          </article>
        ))}
      </section>

      <section className="grid grid-cols-3 gap-3">
        <div className="col-span-2 space-y-3">
          <div className="card p-3">
            <div className="text-sm text-dashboard-muted mb-2">PnL 累计收益曲线</div>
            <PnlAreaChart points={pnl?.rows || []} />
          </div>
          <div className="card p-3">
            <div className="text-sm text-dashboard-muted mb-2">策略收益对比</div>
            <StrategyBarChart rows={pnl?.by_strategy || []} />
          </div>
        </div>

        <aside className="col-span-1 card p-3 flex flex-col">
          <div className="flex items-center justify-between mb-2">
            <div className="text-sm text-dashboard-muted">策略运行面板</div>
            <div className="text-xs text-dashboard-muted">{strategies.length} 个策略</div>
          </div>
          <div className="mb-2 flex items-center justify-between gap-2 text-xs">
            <div className="text-dashboard-muted truncate" title={currentProvider || '无可用 provider'}>
              当前 Provider: <span className="text-dashboard-text">{currentProvider || '无可用 provider'}</span>
            </div>
            <button
              onClick={() => void recheckProviders()}
              disabled={recheckingProvider}
              className="rounded border border-dashboard-line bg-[#111827] px-2 py-1 hover:border-[#4b5563] disabled:opacity-60"
            >
              {recheckingProvider ? '检测中...' : '重新检测'}
            </button>
          </div>
          {llmHealth && !llmHealth.ok ? (
            <div className="mb-2 rounded-lg border border-amber-400/40 bg-amber-300/10 px-2 py-1.5 text-xs text-amber-300 space-y-1.5">
              <div>⚠️ LLM 连接失败: {llmHealth.error || llmHealth.status}</div>
              <div>当前 provider: {currentProvider || '无'}</div>
              {allProviderUnavailable && providerErrors.length > 0 ? (
                <div className="rounded border border-amber-400/30 bg-black/20 p-1.5 text-[11px] space-y-1 max-h-28 overflow-auto">
                  {providerErrors.map((r, idx) => (
                    <div key={`${r.provider_id}-${idx}`}>
                      {r.provider_id || '-'}: {r.error || 'unavailable'}
                    </div>
                  ))}
                </div>
              ) : null}
            </div>
          ) : null}
          <div className="space-y-2 overflow-auto scroll-dark max-h-[610px] pr-1">
            {strategies.map((row) => (
              <StrategyCard key={row.strategy_id} row={row} onToggle={toggleStrategy} onDetail={setDetail} />
            ))}
            {strategies.length === 0 ? (
              <div className="text-sm text-dashboard-muted text-center py-10">暂无策略</div>
            ) : null}
          </div>
        </aside>
      </section>

      <section className="grid grid-cols-3 gap-3">
        <div className="col-span-2 card p-3">
          <div className="flex items-center justify-between mb-2">
            <div className="text-sm text-dashboard-muted">市场监控</div>
            <div className="text-xs text-dashboard-muted">高亮: spread&gt;4% / Yes+No偏离</div>
          </div>
          <div className="overflow-auto scroll-dark max-h-[290px]">
            <table className="w-full text-sm min-w-[900px]">
              <thead className="text-dashboard-muted bg-[#111827]">
                <tr>
                  <th className="text-left px-2 py-2 font-medium">市场</th>
                  <th className="text-left px-2 py-2 font-medium">mid</th>
                  <th className="text-left px-2 py-2 font-medium">spread%</th>
                  <th className="text-left px-2 py-2 font-medium">24h量</th>
                  <th className="text-left px-2 py-2 font-medium">深度</th>
                  <th className="text-left px-2 py-2 font-medium">Yes+No</th>
                </tr>
              </thead>
              <tbody>
                {(markets || []).slice(0, 30).map((m) => (
                  <tr key={m.market_id} className="border-t border-dashboard-line">
                    <td className="px-2 py-2 max-w-[380px] truncate" title={m.name_en || m.name}>{m.name}</td>
                    <td className="px-2 py-2">{Number(m.mid_price || 0).toFixed(4)}</td>
                    <td className={`px-2 py-2 ${m.spread >= 0.04 ? 'text-dashboard-good font-semibold' : ''}`}>{Number(m.spread_pct || 0).toFixed(2)}%</td>
                    <td className="px-2 py-2">{Number(m.volume_24h || 0).toFixed(0)}</td>
                    <td className="px-2 py-2">{Number(m.depth_usdc || 0).toFixed(0)}</td>
                    <td className={`px-2 py-2 ${Math.abs((m.yes_no_sum || 0) - 1.0) >= 0.04 ? 'text-amber-300 font-semibold' : ''}`}>
                      {Number(m.yes_no_sum || 0).toFixed(4)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {markets.length === 0 ? <div className="text-dashboard-muted text-sm py-8 text-center">暂无市场监控数据</div> : null}
          </div>
        </div>

        <div className="col-span-1 card p-3">
          <div className="text-sm text-dashboard-muted mb-2">AI 市场评估</div>
          <div className="overflow-auto scroll-dark max-h-[290px]">
            <table className="w-full text-xs">
              <thead className="text-dashboard-muted bg-[#111827]">
                <tr>
                  <th className="text-left px-2 py-1.5 font-medium">市场</th>
                  <th className="text-left px-2 py-1.5 font-medium">市场价</th>
                  <th className="text-left px-2 py-1.5 font-medium">AI</th>
                  <th className="text-left px-2 py-1.5 font-medium">偏差</th>
                  <th className="text-left px-2 py-1.5 font-medium">置信度</th>
                </tr>
              </thead>
              <tbody>
                {(aiRows || []).slice(0, 20).map((r) => (
                  <tr key={`${r.market_id}-${r.evaluated_at_utc}`} className="border-t border-dashboard-line">
                    <td className="px-2 py-1.5 max-w-[160px] truncate" title={r.name}>{r.name}</td>
                    <td className="px-2 py-1.5">{Number(r.market_yes_mid || 0).toFixed(3)}</td>
                    <td className="px-2 py-1.5">{Number(r.ai_probability || 0).toFixed(3)}</td>
                    <td className={`px-2 py-1.5 ${r.triggered ? 'text-dashboard-good font-semibold' : ''}`}>{(Number(r.deviation || 0) * 100).toFixed(1)}%</td>
                    <td className="px-2 py-1.5">{(Number(r.confidence || 0) * 100).toFixed(0)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {aiRows.length === 0 ? <div className="text-dashboard-muted text-sm py-8 text-center">暂无AI评估（请检查LLM配置）</div> : null}
          </div>
        </div>
      </section>

      <section className="card p-3">
        <div className="flex items-center justify-between mb-2">
          <div className="text-sm text-dashboard-muted">最近交易流水（自动刷新）</div>
          <div className="text-xs text-dashboard-muted">最近 20 条</div>
        </div>
        <div className="overflow-auto scroll-dark">
          <table className="w-full text-sm min-w-[1350px]">
            <thead className="text-dashboard-muted bg-[#111827]">
              <tr>
                <th className="text-left px-3 py-2 font-medium">时间</th>
                <th className="text-left px-3 py-2 font-medium">策略ID</th>
                <th className="text-left px-3 py-2 font-medium">买/卖</th>
                <th className="text-left px-3 py-2 font-medium">市场</th>
                <th className="text-left px-3 py-2 font-medium">价格</th>
                <th className="text-left px-3 py-2 font-medium">数量</th>
                <th className="text-left px-3 py-2 font-medium">决策原因</th>
              </tr>
            </thead>
            <tbody>
              {trades.map((t, idx) => (
                <tr key={`${t.time_utc}-${t.strategy_id}-${idx}`} className="border-t border-dashboard-line">
                  <td className="px-3 py-2 whitespace-nowrap">{toLocalTime(t.time_utc)}</td>
                  <td className="px-3 py-2 font-mono">{t.strategy_id}</td>
                  <td className="px-3 py-2">
                    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${String(t.side).toUpperCase() === 'BUY' ? 'bg-emerald-400/15 text-dashboard-good' : 'bg-rose-400/15 text-dashboard-bad'}`}>
                      {String(t.side).toUpperCase()}
                    </span>
                  </td>
                  <td className="px-3 py-2 max-w-[460px] truncate" title={t.market_name_en || t.market_name}>{t.market_name}</td>
                  <td className="px-3 py-2">{Number(t.price || 0).toFixed(4)}</td>
                  <td className="px-3 py-2">{Number(t.quantity || 0).toFixed(4)}</td>
                  <td className="px-3 py-2 max-w-[460px] truncate" title={t.decision_reason || ''}>{t.decision_reason || '-'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {trades.length === 0 ? <div className="text-dashboard-muted text-sm py-8 text-center">暂无交易记录</div> : null}
        </div>
      </section>

      {loading ? <div className="text-sm text-dashboard-muted">加载中...</div> : null}
      {error ? <div className="text-sm text-dashboard-bad">{error}</div> : null}

      {detail ? (
        <div className="fixed inset-0 z-40 bg-black/60 flex items-center justify-center p-6">
          <div className="w-[520px] card p-4 space-y-4">
            <div className="flex items-center justify-between">
              <div>
                <div className="text-xs text-dashboard-muted">策略详情</div>
                <div className="text-lg font-semibold">{detail.strategy_id}</div>
              </div>
              <button onClick={() => setDetail(null)} className="rounded-lg border border-dashboard-line px-3 py-1.5 bg-[#111827] hover:border-[#4b5563] inline-flex items-center gap-1">
                关闭 <ChevronDown size={14} />
              </button>
            </div>
            <div className="grid grid-cols-2 gap-3 text-sm">
              <div className="card p-3"><div className="text-dashboard-muted text-xs">状态</div><div className="mt-1">{detail.status}</div></div>
              <div className="card p-3"><div className="text-dashboard-muted text-xs">PnL</div><div className={`mt-1 ${detail.total_pnl >= 0 ? 'text-dashboard-good' : 'text-dashboard-bad'}`}>{detail.total_pnl.toFixed(4)}</div></div>
              <div className="card p-3"><div className="text-dashboard-muted text-xs">胜率</div><div className="mt-1">{(detail.win_rate * 100).toFixed(2)}%</div></div>
              <div className="card p-3"><div className="text-dashboard-muted text-xs">交易数</div><div className="mt-1">{detail.trade_count}</div></div>
              <div className="card p-3"><div className="text-dashboard-muted text-xs">最大回撤</div><div className="mt-1">{detail.max_drawdown_pct.toFixed(3)}%</div></div>
              <div className="card p-3"><div className="text-dashboard-muted text-xs">未完成订单</div><div className="mt-1">{detail.open_orders}</div></div>
            </div>
            <div className="text-xs text-dashboard-muted">类型: {detail.strategy_type} | 来源: {detail.source}</div>
            <div className="flex items-center gap-2">
              <button
                onClick={() => void toggleStrategy(detail).then(() => setDetail(null))}
                className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563] inline-flex items-center gap-2"
              >
                {detail.status === 'running' ? <Pause size={14} /> : <Play size={14} />}
                {detail.status === 'running' ? '暂停策略' : '启动策略'}
              </button>
            </div>
          </div>
        </div>
      ) : null}

      <SettingsModal open={settingsOpen} onClose={() => setSettingsOpen(false)} onRefreshMain={loadData} />
    </div>
  );
}
