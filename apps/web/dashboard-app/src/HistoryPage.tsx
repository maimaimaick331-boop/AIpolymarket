import { useCallback, useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { apiGet, toLocalTime } from './lib/api';

interface ArchivedStrategyRow {
  strategy_id: string;
  name: string;
  status: string;
  created_at: string;
  archived_at: string;
  stop_reason: string;
  trade_count: number;
  total_pnl: number;
}

interface ArchivedTradeRow {
  id: number;
  strategy_id: string;
  timestamp: string;
  side: string;
  market: string;
  market_en?: string;
  price: number;
  quantity: number;
  cost_usdc: number;
  pnl: number;
  decision_reason: string;
}

export default function HistoryPage() {
  const [strategies, setStrategies] = useState<ArchivedStrategyRow[]>([]);
  const [trades, setTrades] = useState<ArchivedTradeRow[]>([]);
  const [strategyFilter, setStrategyFilter] = useState('');
  const [error, setError] = useState('');
  const [refreshAt, setRefreshAt] = useState('');

  const loadData = useCallback(async () => {
    try {
      const [s, t] = await Promise.all([
        apiGet<{ rows: ArchivedStrategyRow[] }>('/history/strategies?limit=200'),
        apiGet<{ rows: ArchivedTradeRow[] }>(`/history/trades?limit=600${strategyFilter ? `&strategy_id=${encodeURIComponent(strategyFilter)}` : ''}`),
      ]);
      setStrategies(s.rows || []);
      setTrades(t.rows || []);
      setRefreshAt(new Date().toLocaleString('zh-CN', { hour12: false }));
      setError('');
    } catch (err) {
      setError((err as Error).message || '读取历史失败');
    }
  }, [strategyFilter]);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  return (
    <div className="min-h-screen bg-dashboard-bg text-dashboard-text px-5 py-4 space-y-4">
      <header className="card px-4 py-3 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold m-0">历史归档</h1>
          <div className="text-xs text-dashboard-muted mt-1">已删除策略与归档交易记录</div>
        </div>
        <div className="flex items-center gap-2">
          <a href="/" className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563]">仪表盘</a>
          <a
            href="/live"
            className="rounded-lg border border-orange-500/50 bg-[#111827] px-3 py-2 hover:border-orange-400 inline-flex items-center gap-2 text-orange-300"
          >
            🔴 实盘中心
          </a>
          <a href="/workshop" className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563]">工坊</a>
          <a href="/strategies" className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563]">策略管理</a>
          <a href="/settings" className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563]">设置</a>
          <button onClick={() => void loadData()} className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563] inline-flex items-center gap-1">
            <RefreshCw size={14} />
            刷新
          </button>
        </div>
      </header>

      <section className="card p-3">
        <div className="text-sm text-dashboard-muted mb-2">归档策略</div>
        <div className="overflow-auto scroll-dark max-h-[280px]">
          <table className="w-full text-sm min-w-[980px]">
            <thead className="text-dashboard-muted bg-[#111827]">
              <tr>
                <th className="text-left px-2 py-2 font-medium">策略ID</th>
                <th className="text-left px-2 py-2 font-medium">名称</th>
                <th className="text-left px-2 py-2 font-medium">归档时间</th>
                <th className="text-left px-2 py-2 font-medium">停止原因</th>
                <th className="text-left px-2 py-2 font-medium">交易笔数</th>
                <th className="text-left px-2 py-2 font-medium">总PnL</th>
              </tr>
            </thead>
            <tbody>
              {strategies.map((row) => (
                <tr key={row.strategy_id} className="border-t border-dashboard-line">
                  <td className="px-2 py-2 font-mono">{row.strategy_id}</td>
                  <td className="px-2 py-2">{row.name}</td>
                  <td className="px-2 py-2">{toLocalTime(row.archived_at || row.created_at)}</td>
                  <td className="px-2 py-2">{row.stop_reason || '-'}</td>
                  <td className="px-2 py-2">{row.trade_count}</td>
                  <td className={`px-2 py-2 ${Number(row.total_pnl || 0) >= 0 ? 'text-dashboard-good' : 'text-dashboard-bad'}`}>{Number(row.total_pnl || 0).toFixed(4)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {strategies.length === 0 ? <div className="text-dashboard-muted text-sm py-8 text-center">暂无归档策略</div> : null}
        </div>
      </section>

      <section className="card p-3">
        <div className="flex items-center justify-between mb-2">
          <div className="text-sm text-dashboard-muted">归档交易记录</div>
          <div className="flex items-center gap-2">
            <input
              value={strategyFilter}
              onChange={(e) => setStrategyFilter(e.target.value)}
              placeholder="按策略ID过滤"
              className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 text-xs"
            />
            <button onClick={() => void loadData()} className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563] text-xs">应用筛选</button>
          </div>
        </div>
        <div className="overflow-auto scroll-dark max-h-[340px]">
          <table className="w-full text-sm min-w-[1200px]">
            <thead className="text-dashboard-muted bg-[#111827]">
              <tr>
                <th className="text-left px-2 py-2 font-medium">时间</th>
                <th className="text-left px-2 py-2 font-medium">策略ID</th>
                <th className="text-left px-2 py-2 font-medium">方向</th>
                <th className="text-left px-2 py-2 font-medium">市场</th>
                <th className="text-left px-2 py-2 font-medium">价格</th>
                <th className="text-left px-2 py-2 font-medium">数量</th>
                <th className="text-left px-2 py-2 font-medium">花费USDC</th>
                <th className="text-left px-2 py-2 font-medium">盈亏</th>
                <th className="text-left px-2 py-2 font-medium">决策理由</th>
              </tr>
            </thead>
            <tbody>
              {trades.map((row) => (
                <tr key={row.id} className="border-t border-dashboard-line">
                  <td className="px-2 py-2 whitespace-nowrap">{toLocalTime(row.timestamp)}</td>
                  <td className="px-2 py-2 font-mono">{row.strategy_id}</td>
                  <td className="px-2 py-2">{String(row.side || '').toUpperCase()}</td>
                  <td className="px-2 py-2 max-w-[220px] truncate" title={row.market_en || row.market}>{row.market}</td>
                  <td className="px-2 py-2">{Number(row.price || 0).toFixed(4)}</td>
                  <td className="px-2 py-2">{Number(row.quantity || 0).toFixed(4)}</td>
                  <td className="px-2 py-2">{Number(row.cost_usdc || 0).toFixed(4)}</td>
                  <td className={`px-2 py-2 ${Number(row.pnl || 0) >= 0 ? 'text-dashboard-good' : 'text-dashboard-bad'}`}>{Number(row.pnl || 0).toFixed(4)}</td>
                  <td className="px-2 py-2 max-w-[320px] truncate" title={row.decision_reason || ''}>{row.decision_reason || '-'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {trades.length === 0 ? <div className="text-dashboard-muted text-sm py-8 text-center">暂无归档交易</div> : null}
        </div>
      </section>

      <div className="text-xs text-dashboard-muted">最近刷新: {refreshAt || '-'}</div>
      {error ? <div className="text-sm text-dashboard-bad">{error}</div> : null}
    </div>
  );
}
