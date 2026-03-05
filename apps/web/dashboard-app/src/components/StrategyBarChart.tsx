import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import type { StrategyPnlItem } from '../types';

interface StrategyBarChartProps {
  rows: StrategyPnlItem[];
}

export default function StrategyBarChart({ rows }: StrategyBarChartProps) {
  const data = rows.slice(0, 12).map((x) => ({ ...x, short: x.strategy_id.slice(0, 10) }));

  if (data.length === 0) {
    return (
      <div className="card h-[220px] flex items-center justify-center text-dashboard-muted text-sm">
        暂无策略收益对比数据
      </div>
    );
  }

  return (
    <div className="card h-[220px] p-3">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 10, right: 12, left: 0, bottom: 0 }}>
          <CartesianGrid stroke="#273446" strokeDasharray="3 3" />
          <XAxis dataKey="short" stroke="#9ca3af" tickLine={false} axisLine={{ stroke: '#374151' }} />
          <YAxis stroke="#9ca3af" tickLine={false} axisLine={{ stroke: '#374151' }} />
          <Tooltip
            cursor={{ fill: 'rgba(255,255,255,0.03)' }}
            contentStyle={{ background: '#111827', border: '1px solid #374151', color: '#e5e7eb' }}
            formatter={(v: number) => v.toFixed(4)}
            labelFormatter={(label) => `策略: ${label}`}
          />
          <Bar dataKey="pnl" radius={[6, 6, 0, 0]}>
            {data.map((entry) => (
              <Cell key={entry.strategy_id} fill={entry.pnl >= 0 ? '#4ade80' : '#f87171'} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
