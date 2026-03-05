import { useEffect, useMemo, useRef } from 'react';
import {
  AreaSeries,
  ColorType,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts';
import type { PnlPoint } from '../types';

interface PnlAreaChartProps {
  points: PnlPoint[];
}

function toTs(iso: string): UTCTimestamp | null {
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return null;
  return Math.floor(ms / 1000) as UTCTimestamp;
}

export default function PnlAreaChart({ points }: PnlAreaChartProps) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<'Area'> | null>(null);

  const data = useMemo(() => {
    const out: { time: Time; value: number }[] = [];
    for (const p of points) {
      const ts = toTs(p.time_utc);
      if (ts === null) continue;
      out.push({ time: ts, value: Number(p.value) || 0 });
    }
    return out;
  }, [points]);

  useEffect(() => {
    const el = rootRef.current;
    if (!el) return;

    const chart = createChart(el, {
      layout: {
        background: { type: ColorType.Solid, color: '#1f2937' },
        textColor: '#9ca3af',
      },
      grid: {
        vertLines: { color: '#273446' },
        horzLines: { color: '#273446' },
      },
      rightPriceScale: {
        borderColor: '#374151',
      },
      timeScale: {
        borderColor: '#374151',
        timeVisible: true,
        secondsVisible: false,
      },
      crosshair: {
        vertLine: { color: '#4b5563' },
        horzLine: { color: '#4b5563' },
      },
      width: el.clientWidth,
      height: 280,
    });

    const series = chart.addSeries(AreaSeries, {
      topColor: 'rgba(74,222,128,0.4)',
      bottomColor: 'rgba(74,222,128,0.03)',
      lineColor: '#4ade80',
      lineWidth: 2,
      priceLineVisible: true,
    });

    chartRef.current = chart;
    seriesRef.current = series;

    const resize = () => {
      chart.applyOptions({ width: el.clientWidth });
      chart.timeScale().fitContent();
    };

    const observer = new ResizeObserver(resize);
    observer.observe(el);

    return () => {
      observer.disconnect();
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!seriesRef.current) return;
    seriesRef.current.setData(data);
    chartRef.current?.timeScale().fitContent();
  }, [data]);

  if (data.length === 0) {
    return (
      <div className="card h-[280px] flex items-center justify-center text-dashboard-muted text-sm">
        暂无 PnL 历史数据
      </div>
    );
  }

  return <div ref={rootRef} className="card h-[280px] p-2" />;
}
