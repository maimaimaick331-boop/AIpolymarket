from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import webbrowser
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

if __package__ in (None, ''):
    # Allow running as: python apps/trader/run_race_viz.py
    sys.path.append(str(Path(__file__).resolve().parents[2]))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='生成策略赛马可视化看板。')
    parser.add_argument('--race-summary', default='data/raw/polymarket/paper/race/race_latest.json', help='赛马汇总 JSON。')
    parser.add_argument('--output', default='data/raw/polymarket/paper/race/race_report_latest.html', help='输出 HTML。')
    parser.add_argument('--serve', action='store_true', help='启动本地服务。')
    parser.add_argument('--port', type=int, default=8770, help='服务端口。')
    parser.add_argument('--open-browser', action='store_true', help='自动打开浏览器。')
    parser.add_argument('--auto-refresh-sec', type=int, default=5, help='自动刷新秒数，0关闭。')
    return parser.parse_args()


def _html(race: dict, source_path: Path, auto_refresh_sec: int) -> str:
    data_json = json.dumps(race, ensure_ascii=False)
    refresh_js = '' if auto_refresh_sec <= 0 else f"setTimeout(() => window.location.reload(), {auto_refresh_sec * 1000});"
    return f"""<!doctype html>
<html lang=\"zh-CN\"><head><meta charset=\"utf-8\"/><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"/>
<title>策略赛马看板</title>
<style>
:root{{--bg:#f6f8fc;--card:#fff;--ink:#10243a;--muted:#62788f;--line:#dce6f1;--ok:#2b8a3e;--bad:#c92a2a;}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);font-family:"PingFang SC","Microsoft YaHei",sans-serif;color:var(--ink)}}
.wrap{{max-width:1280px;margin:20px auto;padding:0 16px}}
.hero{{background:linear-gradient(120deg,#0f766e,#1d3557);color:#e8f6ff;padding:16px;border-radius:16px}}
.grid{{display:grid;grid-template-columns:1.3fr 1fr;gap:12px;margin-top:12px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}}
.hd{{padding:10px 12px;border-bottom:1px solid var(--line);font-size:13px;color:var(--muted);font-weight:600}}
.bd{{padding:12px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}th,td{{padding:8px;border-bottom:1px solid var(--line);text-align:left}}th{{background:#f5f8fb;position:sticky;top:0}}
.mono{{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}}.pos{{color:var(--ok)}}.neg{{color:var(--bad)}}
.item{{border:1px solid var(--line);border-radius:10px;padding:10px;margin-bottom:8px;background:#fbfdff}}
.tag{{display:inline-block;padding:2px 8px;border-radius:999px;background:#edf2f7;font-size:12px;margin-left:6px}}
.small{{font-size:12px;color:var(--muted)}}
@media(max-width:980px){{.grid{{grid-template-columns:1fr}}}}
</style></head>
<body><div class=\"wrap\">
<div class=\"hero\"><h2 style=\"margin:0\">AI 策略赛马看板</h2><div class=\"small\">来源: <span class=\"mono\">{source_path}</span></div></div>
<div class=\"grid\">
<div class=\"card\"><div class=\"hd\">排行榜</div><div class=\"bd\"><table><thead><tr><th>排名</th><th>ID</th><th>名称</th><th>类型</th><th>Score</th><th>PnL</th><th>回撤</th><th>胜率</th><th>熔断</th></tr></thead><tbody id=\"leaderBody\"></tbody></table></div></div>
<div class=\"card\"><div class=\"hd\">赛马概览</div><div class=\"bd\" id=\"meta\"></div></div>
</div>
<div class=\"card\" style=\"margin-top:12px\"><div class=\"hd\">策略设定与最近成交（每个策略独立）</div><div class=\"bd\" id=\"runs\"></div></div>
</div>
<script>
const race={data_json};
const lb=Array.isArray(race.leaderboard)?race.leaderboard:[];
const runs=Array.isArray(race.runs)?race.runs:[];
const leaderBody=document.getElementById('leaderBody');
lb.forEach((r,i)=>{{
 const tr=document.createElement('tr');
 const pnl=Number(r.pnl||0),dd=Number(r.max_drawdown_pct||0),wr=Number(r.win_rate||0)*100;
 tr.innerHTML=`<td>${{i+1}}</td><td class=\"mono\">${{r.strategy_id}}</td><td>${{r.name}}</td><td>${{r.type}}</td><td>${{Number(r.score||0).toFixed(4)}}</td><td class=\"${{pnl>=0?'pos':'neg'}}\">${{pnl.toFixed(4)}}</td><td>${{dd.toFixed(3)}}%</td><td>${{wr.toFixed(1)}}%</td><td>${{r.risk_halted?'是':'否'}}</td>`;
 leaderBody.appendChild(tr);
}});
const meta=document.getElementById('meta');
meta.innerHTML=`<div>候选策略: <b>${{race.candidates||0}}</b></div><div>快照数: <b>${{race.snapshot_count||0}}</b></div><div>Token数: <b>${{(race.token_universe||[]).length}}</b></div><div>开始: <span class=\"mono\">${{race.started_at_utc||''}}</span></div><div>结束: <span class=\"mono\">${{race.finished_at_utc||''}}</span></div>`;
const runsEl=document.getElementById('runs');
runs.forEach((item)=>{{
 const s=item.strategy||{{}},m=item.metrics||{{}},fills=(item.recent_fills||[]).slice(-8).reverse();
 const box=document.createElement('div'); box.className='item';
 const settings=Object.entries(s.params||{{}}).map(([k,v])=>`${{k}}=${{v}}`).join(' | ');
 const fillRows=fills.map(f=>`<tr><td>${{f.tick}}</td><td>${{String(f.side||'').toUpperCase()}}</td><td class=\"mono\">${{String(f.token_id||'').slice(0,12)}}...</td><td>${{Number(f.quantity||0).toFixed(3)}}</td><td>${{Number(f.price||0).toFixed(4)}}</td><td>${{String(f.filled_at_utc||'')}}</td></tr>`).join('');
 box.innerHTML=`<div><b>${{s.name||''}}</b><span class=\"tag\">${{s.strategy_id||''}}</span><span class=\"tag\">${{s.strategy_type||''}}</span><span class=\"tag\">${{s.source||''}}</span></div><div class=\"small\" style=\"margin:6px 0\">设定: ${{settings||'-'}}</div><div class=\"small\">PnL=${{Number(m.realized_pnl||0).toFixed(4)}} | 回撤=${{Number(m.max_drawdown_pct||0).toFixed(3)}}% | 胜率=${{(Number(m.win_rate||0)*100).toFixed(1)}}% | 成交=${{m.fills_count||0}}</div><div style=\"margin-top:8px\"><table><thead><tr><th>Tick</th><th>方向</th><th>Token</th><th>数量</th><th>价格</th><th>时间</th></tr></thead><tbody>${{fillRows||'<tr><td colspan="6">暂无成交</td></tr>'}}</tbody></table></div>`;
 runsEl.appendChild(box);
}});
{refresh_js}
</script></body></html>
"""


def main() -> int:
    args = parse_args()
    source = Path(args.race_summary)
    if not source.exists():
        print(f'未找到赛马汇总文件: {source}')
        return 1

    race = json.loads(source.read_text(encoding='utf-8'))
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_html(race, source, auto_refresh_sec=max(0, args.auto_refresh_sec)), encoding='utf-8')
    print(f'赛马看板={out}')

    if args.serve:
        cwd = Path.cwd()
        try:
            import os

            os.chdir(out.parent)
            server = ThreadingHTTPServer(('127.0.0.1', args.port), SimpleHTTPRequestHandler)
            url = f'http://127.0.0.1:{args.port}/{out.name}'
            print(f'本地访问地址={url}')
            if args.open_browser:
                webbrowser.open(url)
            server.serve_forever()
        finally:
            os.chdir(cwd)

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
