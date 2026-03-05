from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import webbrowser
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

if __package__ in (None, ''):
    # Allow running as: python apps/trader/run_paper_viz.py
    sys.path.append(str(Path(__file__).resolve().parents[2]))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='生成并展示模拟盘回放网页看板。')
    parser.add_argument('--paper-dir', default='data/raw/polymarket/paper', help='包含 paper_summary_* 文件的目录。')
    parser.add_argument('--summary', default='', help='指定 summary JSON 文件路径；为空时自动取最新。')
    parser.add_argument('--fills', default='', help='指定 fills JSONL 文件路径；为空时按 summary 自动推断。')
    parser.add_argument('--output', default='', help='输出 HTML 路径；默认 <paper-dir>/report_latest.html')
    parser.add_argument('--serve', action='store_true', help='通过本地 HTTP 服务展示网页。')
    parser.add_argument('--port', type=int, default=8765, help='--serve 时使用的端口。')
    parser.add_argument('--open-browser', action='store_true', help='--serve 时自动打开浏览器。')
    parser.add_argument('--auto-refresh-sec', type=int, default=5, help='页面自动刷新秒数，0 表示关闭。')
    return parser.parse_args()


def _latest_summary(paper_dir: Path) -> Path:
    summaries = sorted(paper_dir.glob('paper_summary_*.json'))
    if not summaries:
        raise FileNotFoundError(f'在 {paper_dir} 下未找到 summary 文件')
    return summaries[-1]


def _infer_fills(summary_path: Path) -> Path:
    name = summary_path.name
    slug = name.replace('paper_summary_', '').replace('.json', '')
    return summary_path.parent / f'paper_fills_{slug}.jsonl'


def _load_summary(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def _load_fills(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _diagnosis(summary: dict) -> list[str]:
    items: list[str] = []
    pnl = float(summary.get('realized_pnl', 0.0))
    dd = float(summary.get('max_drawdown_pct', 0.0))
    win_rate = float(summary.get('win_rate', 0.0))
    trade_count = int(summary.get('trade_count', summary.get('fills_count', 0)))

    if trade_count < 20:
        items.append('样本量偏小，先拉长回放窗口到 200+ 快照再评价策略。')
    if pnl < 0:
        items.append('当前策略净收益为负，优先检查进出场时机和手续费占比。')
    if dd > 2.0:
        items.append('最大回撤偏高，建议先收紧单次下单量和单市场限额。')
    if win_rate < 0.45:
        items.append('卖出胜率偏低，建议增加趋势过滤或减少逆势交易。')
    if bool(summary.get('risk_halted', False)):
        items.append('本轮回放触发了风险熔断，建议下调仓位并增加止损约束。')
    if not items:
        items.append('当前样本下策略表现稳定，可以开始做参数网格测试。')
    return items


def _html(summary: dict, fills: list[dict], source_summary: Path, source_fills: Path, auto_refresh_sec: int) -> str:
    equity = summary.get('equity_curve', [])
    token_stats = summary.get('token_stats', {}) if isinstance(summary.get('token_stats'), dict) else {}
    diagnosis = _diagnosis(summary)

    stats = {
        '快照数量': summary.get('snapshot_count', 0),
        '成交笔数': summary.get('fills_count', 0),
        '策略': summary.get('strategy', 'periodic'),
        '胜率(卖出)': f"{float(summary.get('win_rate', 0.0)) * 100:.2f}%",
        '最大回撤': f"{float(summary.get('max_drawdown_pct', 0.0)):.3f}%",
        '总手续费': round(float(summary.get('total_fees', 0.0)), 6),
        '总换手': round(float(summary.get('turnover', 0.0)), 4),
        '期末权益': round(float(summary.get('final_equity', 0.0)), 4),
        '收益(PnL)': round(float(summary.get('realized_pnl', 0.0)), 4),
    }

    payload = {
        'summary': summary,
        'fills': fills,
        'equity': equity,
        'stats': stats,
        'tokenStats': token_stats,
        'diagnosis': diagnosis,
        'riskEvents': summary.get('risk_events', []),
    }

    data_json = json.dumps(payload, ensure_ascii=False)
    refresh_js = ''
    if auto_refresh_sec > 0:
        refresh_js = f"setTimeout(() => window.location.reload(), {auto_refresh_sec * 1000});"

    return f"""<!doctype html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Polymarket 模拟盘诊断看板</title>
  <style>
    :root {{
      --bg: #f2f6fb;
      --card: #ffffff;
      --ink: #11253a;
      --muted: #587089;
      --line: #d9e3ee;
      --accent: #0f7c73;
      --neg: #c02f2f;
      --pos: #208b3a;
      --shadow: 0 10px 30px rgba(18, 42, 66, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; color: var(--ink); background: radial-gradient(1000px 400px at 10% -10%, #d8f2ef 0%, transparent 60%), radial-gradient(700px 300px at 90% 0%, #ffe9d4 0%, transparent 60%), var(--bg); font-family: "PingFang SC", "Microsoft YaHei", "Noto Sans SC", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    .wrap {{ max-width: 1280px; margin: 20px auto 40px; padding: 0 16px; }}
    .hero {{ background: linear-gradient(120deg, #0f7c73 0%, #15616d 45%, #2f4858 100%); color: #eaf8f7; border-radius: 18px; box-shadow: var(--shadow); padding: 18px 20px; margin-bottom: 14px; }}
    .hero h1 {{ margin: 0; font-size: 26px; }}
    .hero p {{ margin: 6px 0 0; color: #cde9e6; font-size: 13px; }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
    .grid-kpi {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }}
    .kpi {{ background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 14px; box-shadow: var(--shadow); }}
    .kpi .k {{ font-size: 12px; color: var(--muted); margin-bottom: 8px; }}
    .kpi .v {{ font-size: 22px; font-weight: 700; }}
    .pos {{ color: var(--pos); }} .neg {{ color: var(--neg); }}
    .grid-main {{ display: grid; grid-template-columns: 2fr 1fr; gap: 12px; margin-top: 12px; }}
    .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 14px; box-shadow: var(--shadow); overflow: hidden; }}
    .card .hd {{ padding: 12px 14px; border-bottom: 1px solid var(--line); font-size: 13px; color: var(--muted); font-weight: 600; }}
    .card .bd {{ padding: 12px; }}
    .risk-banner {{ background: #fff4e6; border: 1px solid #ffd8a8; border-radius: 10px; padding: 10px; font-size: 12px; margin-bottom: 10px; }}
    .chart-wrap {{ display: grid; gap: 10px; }}
    canvas {{ width: 100%; border: 1px solid var(--line); border-radius: 10px; background: #fbfdff; }}
    #equityChart {{ height: 280px; }} #drawdownChart {{ height: 160px; }}
    .diag {{ list-style: none; margin: 0; padding: 0; display: grid; gap: 8px; }}
    .diag li {{ background: #f9fbff; border: 1px dashed #cfdced; border-radius: 10px; padding: 10px; font-size: 13px; }}
    .toolbar {{ display: flex; gap: 8px; align-items: center; margin-bottom: 10px; flex-wrap: wrap; }}
    .toolbar input, .toolbar select {{ border: 1px solid var(--line); border-radius: 8px; padding: 7px 10px; font-size: 13px; background: #fff; }}
    .toolbar button {{ border: none; background: var(--accent); color: white; border-radius: 8px; padding: 8px 11px; font-size: 13px; cursor: pointer; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ text-align: left; border-bottom: 1px solid var(--line); padding: 9px; }}
    th {{ background: #f4f8fc; color: #355069; position: sticky; top: 0; z-index: 1; }}
    .table-wrap {{ max-height: 300px; overflow: auto; border: 1px solid var(--line); border-radius: 10px; }}
    .badge {{ display: inline-block; border-radius: 999px; padding: 2px 8px; font-size: 12px; }}
    .buy {{ background: #d7f5e0; color: #1b6b33; }} .sell {{ background: #ffe0e0; color: #8f2222; }}
    .grid-bottom {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 12px; }}
    @media (max-width: 980px) {{ .grid-kpi {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} .grid-main,.grid-bottom {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <div class=\"wrap\">
    <div class=\"hero\"><h1>Polymarket 模拟盘诊断看板</h1><p>汇总文件: <span class=\"mono\">{source_summary}</span><br/>成交文件: <span class=\"mono\">{source_fills}</span></p></div>
    <div id=\"kpi\" class=\"grid-kpi\"></div>
    <div class=\"grid-main\">
      <div class=\"card\"><div class=\"hd\">权益与回撤</div><div class=\"bd chart-wrap\"><canvas id=\"equityChart\" width=\"920\" height=\"280\"></canvas><canvas id=\"drawdownChart\" width=\"920\" height=\"160\"></canvas></div></div>
      <div class=\"card\"><div class=\"hd\">策略诊断建议</div><div class=\"bd\"><div id=\"riskBanner\"></div><ul id=\"diagList\" class=\"diag\"></ul></div></div>
    </div>
    <div class=\"grid-bottom\">
      <div class=\"card\"><div class=\"hd\">Token 盈亏贡献</div><div class=\"bd\"><canvas id=\"tokenPnlChart\" width=\"580\" height=\"280\"></canvas><div class=\"table-wrap\" style=\"margin-top:10px;\"><table><thead><tr><th>Token</th><th>买入量</th><th>卖出量</th><th>手续费</th><th>已实现盈亏</th><th>净仓位</th></tr></thead><tbody id=\"tokenBody\"></tbody></table></div></div></div>
      <div class=\"card\"><div class=\"hd\">成交明细</div><div class=\"bd\"><div class=\"toolbar\"><select id=\"sideFilter\"><option value=\"all\">全部方向</option><option value=\"buy\">仅买入</option><option value=\"sell\">仅卖出</option></select><input id=\"tokenFilter\" placeholder=\"筛选 token 前缀\" /><button id=\"btnReset\" type=\"button\">重置筛选</button></div><div class=\"table-wrap\"><table><thead><tr><th>Tick</th><th>方向</th><th>Token</th><th>数量</th><th>价格</th><th>名义金额</th><th>手续费</th><th>时间</th></tr></thead><tbody id=\"fillsBody\"></tbody></table></div></div></div>
    </div>
  </div>
  <script>
    const data = {data_json};
    const summary = data.summary || {{}};
    const fills = Array.isArray(data.fills) ? data.fills : [];
    const equity = Array.isArray(data.equity) ? data.equity : [];
    const stats = data.stats || {{}};
    const tokenStats = data.tokenStats || {{}};
    const diagnosis = Array.isArray(data.diagnosis) ? data.diagnosis : [];
    const riskEvents = Array.isArray(data.riskEvents) ? data.riskEvents : [];

    const kpiEl = document.getElementById('kpi');
    Object.entries(stats).forEach(([k, v]) => {{
      const div = document.createElement('div');
      div.className = 'kpi';
      const num = parseFloat(String(v).replace('%', ''));
      const cls = k === '收益(PnL)' ? (num >= 0 ? 'pos' : 'neg') : '';
      div.innerHTML = `<div class=\"k\">${{k}}</div><div class=\"v ${{cls}}\">${{v}}</div>`;
      kpiEl.appendChild(div);
    }});

    const diagEl = document.getElementById('diagList');
    diagnosis.forEach((msg) => {{ const li = document.createElement('li'); li.textContent = msg; diagEl.appendChild(li); }});

    if (riskEvents.length) {{
      const banner = document.getElementById('riskBanner');
      const box = document.createElement('div');
      box.className = 'risk-banner';
      box.innerHTML = '<b>风险事件</b><br/>' + riskEvents.map(x => `- ${{x}}`).join('<br/>');
      banner.appendChild(box);
    }}

    function drawLine(canvasId, series, color, textPrefix, fillArea) {{
      const canvas = document.getElementById(canvasId); const ctx = canvas.getContext('2d'); const W = canvas.width; const H = canvas.height; const pad = 28; ctx.clearRect(0, 0, W, H); if (!series.length) return;
      const min = Math.min(...series); const max = Math.max(...series); const range = Math.max(max - min, 1e-9);
      const x = (i) => pad + (i / Math.max(series.length - 1, 1)) * (W - 2 * pad); const y = (v) => H - pad - ((v - min) / range) * (H - 2 * pad);
      ctx.strokeStyle = '#e5edf5'; for (let i = 0; i < 5; i++) {{ const yy = pad + ((H - 2 * pad) / 4) * i; ctx.beginPath(); ctx.moveTo(pad, yy); ctx.lineTo(W - pad, yy); ctx.stroke(); }}
      if (fillArea) {{ const g = ctx.createLinearGradient(0, pad, 0, H - pad); g.addColorStop(0, color + '66'); g.addColorStop(1, color + '08'); ctx.beginPath(); series.forEach((v, i) => {{ const px = x(i), py = y(v); if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py); }}); ctx.lineTo(x(series.length - 1), H - pad); ctx.lineTo(x(0), H - pad); ctx.closePath(); ctx.fillStyle = g; ctx.fill(); }}
      ctx.beginPath(); series.forEach((v, i) => {{ const px = x(i), py = y(v); if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py); }}); ctx.lineWidth = 2.2; ctx.strokeStyle = color; ctx.stroke();
      const last = series[series.length - 1]; ctx.fillStyle = '#415a72'; ctx.font = '12px sans-serif'; ctx.fillText(`${{textPrefix}} 最低=${{min.toFixed(4)}} 最高=${{max.toFixed(4)}} 最新=${{last.toFixed(4)}}`, pad, 18);
    }}

    function drawTokenBars(canvasId, rows) {{
      const canvas = document.getElementById(canvasId); const ctx = canvas.getContext('2d'); const W = canvas.width; const H = canvas.height; const pad = 28; ctx.clearRect(0, 0, W, H); if (!rows.length) return;
      const values = rows.map(r => Number(r.realized_pnl || 0)); const maxAbs = Math.max(...values.map(v => Math.abs(v)), 1e-9); const rowH = (H - 2 * pad) / rows.length; ctx.font = '11px sans-serif';
      rows.forEach((r, i) => {{ const y = pad + i * rowH + rowH * 0.2; const v = Number(r.realized_pnl || 0); const barW = (Math.abs(v) / maxAbs) * (W - 220); const x0 = 170; ctx.fillStyle = '#5e7791'; ctx.fillText(String(r.token_id || '').slice(0, 14) + '...', 8, y + 12); ctx.fillStyle = v >= 0 ? '#2f9e44' : '#d6336c'; ctx.fillRect(x0, y, barW, rowH * 0.55); ctx.fillStyle = '#31485f'; ctx.fillText(v.toFixed(4), x0 + barW + 8, y + 12); }});
    }}

    const eqSeries = equity.map((x) => Number(x.equity || 0));
    drawLine('equityChart', eqSeries, '#0f7c73', '权益', true);
    const ddSeries = []; let peak = -Infinity; for (const v of eqSeries) {{ if (v > peak) peak = v; ddSeries.push(peak > 0 ? ((peak - v) / peak) * 100 : 0); }}
    drawLine('drawdownChart', ddSeries, '#d6336c', '回撤(%)', false);

    const tokenRows = Object.values(tokenStats).filter((x) => x && typeof x === 'object').sort((a, b) => Number(b.realized_pnl || 0) - Number(a.realized_pnl || 0));
    drawTokenBars('tokenPnlChart', tokenRows.slice(0, 12));

    const tokenBody = document.getElementById('tokenBody');
    tokenRows.forEach((r) => {{ const tr = document.createElement('tr'); const pnl = Number(r.realized_pnl || 0); tr.innerHTML = `<td class=\"mono\">${{String(r.token_id || '').slice(0, 14)}}...</td><td>${{Number(r.buy_qty || 0).toFixed(3)}}</td><td>${{Number(r.sell_qty || 0).toFixed(3)}}</td><td>${{Number(r.fees || 0).toFixed(6)}}</td><td class=\"${{pnl >= 0 ? 'pos' : 'neg'}}\">${{pnl.toFixed(4)}}</td><td>${{Number(r.net_position || 0).toFixed(4)}}</td>`; tokenBody.appendChild(tr); }});

    const fillsBody = document.getElementById('fillsBody'); const sideFilter = document.getElementById('sideFilter'); const tokenFilter = document.getElementById('tokenFilter'); const btnReset = document.getElementById('btnReset');
    function renderFills() {{ const side = sideFilter.value; const tokenPrefix = tokenFilter.value.trim().toLowerCase(); const rows = fills.filter((f) => side === 'all' ? true : String(f.side || '').toLowerCase() === side).filter((f) => tokenPrefix ? String(f.token_id || '').toLowerCase().startsWith(tokenPrefix) : true).slice(-200).reverse(); fillsBody.innerHTML = ''; rows.forEach((f) => {{ const s = String(f.side || '').toLowerCase(); const tr = document.createElement('tr'); tr.innerHTML = `<td>${{f.tick ?? ''}}</td><td><span class=\"badge ${{s}}\">${{s === 'buy' ? '买入' : '卖出'}}</span></td><td class=\"mono\">${{String(f.token_id || '').slice(0, 14)}}...</td><td>${{Number(f.quantity || 0).toFixed(4)}}</td><td>${{Number(f.price || 0).toFixed(4)}}</td><td>${{Number(f.notional || 0).toFixed(4)}}</td><td>${{Number(f.fee || 0).toFixed(6)}}</td><td class=\"mono\">${{String(f.filled_at_utc || '')}}</td>`; fillsBody.appendChild(tr); }}); }}
    sideFilter.addEventListener('change', renderFills); tokenFilter.addEventListener('input', renderFills); btnReset.addEventListener('click', () => {{ sideFilter.value = 'all'; tokenFilter.value = ''; renderFills(); }}); renderFills();
    {refresh_js}
  </script>
</body>
</html>
"""


def main() -> int:
    args = parse_args()
    paper_dir = Path(args.paper_dir)
    summary_path = Path(args.summary) if args.summary else _latest_summary(paper_dir)
    fills_path = Path(args.fills) if args.fills else _infer_fills(summary_path)

    summary = _load_summary(summary_path)
    fills = _load_fills(fills_path)

    output = Path(args.output) if args.output else (paper_dir / 'report_latest.html')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_html(summary, fills, summary_path, fills_path, auto_refresh_sec=max(0, args.auto_refresh_sec)), encoding='utf-8')
    print(f'报告文件={output}')

    if args.serve:
        root = output.parent.resolve()
        rel = output.name
        handler = SimpleHTTPRequestHandler

        cwd = Path.cwd()
        try:
            import os

            os.chdir(root)
            server = ThreadingHTTPServer(('127.0.0.1', args.port), handler)
            url = f'http://127.0.0.1:{args.port}/{rel}'
            print(f'本地访问地址={url}')
            if args.open_browser:
                webbrowser.open(url)
            server.serve_forever()
        finally:
            os.chdir(cwd)

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
