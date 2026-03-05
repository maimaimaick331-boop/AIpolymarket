import { PauseCircle, PlayCircle } from 'lucide-react';
import AnimatedNumber from './AnimatedNumber';
import type { StrategyRow } from '../types';

interface StrategyCardProps {
  row: StrategyRow;
  onToggle: (row: StrategyRow) => void;
  onDetail: (row: StrategyRow) => void;
}

function statusStyle(status: StrategyRow['status']) {
  if (status === 'running') return { dot: 'bg-dashboard-good animate-pulseRing', text: 'text-dashboard-good', label: '运行' };
  if (status === 'paused') return { dot: 'bg-yellow-400', text: 'text-yellow-300', label: '暂停' };
  return { dot: 'bg-dashboard-bad', text: 'text-dashboard-bad', label: '停止' };
}

export default function StrategyCard({ row, onToggle, onDetail }: StrategyCardProps) {
  const st = statusStyle(row.status);
  const pnlPositive = Number(row.total_pnl) >= 0;
  const winPct = Math.max(0, Math.min(100, Number(row.win_rate || 0) * 100));

  return (
    <div className="card p-3 space-y-3">
      <div className="flex items-center justify-between">
        <div className="min-w-0">
          <div className="text-sm text-dashboard-muted">{row.strategy_id}</div>
          <div className="font-semibold truncate">{row.name || row.strategy_id}</div>
        </div>
        <div className="flex items-center gap-2">
          <span className={`h-2.5 w-2.5 rounded-full ${st.dot}`} />
          <span className={`text-xs ${st.text}`}>{st.label}</span>
        </div>
      </div>

      <div className={`text-xl font-bold ${pnlPositive ? 'text-dashboard-good' : 'text-dashboard-bad'}`}>
        <AnimatedNumber value={Number(row.total_pnl) || 0} digits={4} />
      </div>

      <div>
        <div className="flex items-center justify-between text-xs text-dashboard-muted mb-1">
          <span>胜率</span>
          <span>{winPct.toFixed(1)}%</span>
        </div>
        <div className="h-2 rounded-full bg-[#111827] overflow-hidden border border-dashboard-line">
          <div
            className={`h-full transition-all duration-500 ${winPct >= 50 ? 'bg-dashboard-good' : 'bg-dashboard-bad'}`}
            style={{ width: `${winPct}%` }}
          />
        </div>
      </div>

      <div className="grid grid-cols-2 gap-2 text-xs text-dashboard-muted">
        <div>交易次数: <span className="text-dashboard-text">{row.trade_count}</span></div>
        <div>最大回撤: <span className="text-dashboard-text">{Number(row.max_drawdown_pct || 0).toFixed(3)}%</span></div>
      </div>

      {row.llm_warning ? (
        <div className="rounded-lg border border-amber-400/40 bg-amber-300/10 px-2 py-1.5 text-xs text-amber-300">
          ⚠️ LLM 连接失败，请检查 Provider / API Key
        </div>
      ) : null}

      <div className="rounded-lg border border-dashboard-line bg-[#111827] px-2 py-2 space-y-1">
        <div className="text-[11px] text-dashboard-muted">最近信号</div>
        {(row.recent_signals || []).length > 0 ? (
          (row.recent_signals || []).slice(0, 3).map((s, idx) => (
            <div key={`${s.time_utc}-${idx}`} className="text-[11px] text-dashboard-muted">
              <span className="text-dashboard-text">{(s.time_utc || '').slice(11, 19) || '--:--:--'}</span>
              {' | '}
              <span className="text-dashboard-text">{s.side}</span>
              {' | '}
              <span>{s.reason || s.signal_type || '-'}</span>
              {' | '}
              <span className={s.status === 'executed' ? 'text-dashboard-good' : 'text-yellow-300'}>
                {s.status === 'executed' ? '已成交' : s.status || '未成交'}
              </span>
            </div>
          ))
        ) : (
          <div className="text-[11px] text-dashboard-muted">暂无信号</div>
        )}
      </div>

      <div className="flex items-center gap-2 pt-1">
        <button
          onClick={() => onToggle(row)}
          className="flex-1 rounded-lg border border-dashboard-line bg-[#111827] px-2 py-1.5 text-sm hover:border-[#4b5563]"
        >
          <span className="inline-flex items-center gap-1">
            {row.status === 'running' ? <PauseCircle size={16} /> : <PlayCircle size={16} />}
            {row.status === 'running' ? '暂停' : '启动'}
          </span>
        </button>
        <button
          onClick={() => onDetail(row)}
          className="rounded-lg border border-dashboard-line bg-[#111827] px-2 py-1.5 text-sm hover:border-[#4b5563]"
        >
          详情
        </button>
      </div>
    </div>
  );
}
