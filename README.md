# Saima Polymarket OpenClaw

当前可运行能力：
1. Polymarket 只读行情抓取  
2. 模拟盘回放（支持策略切换 + 风险熔断）  
3. 可视化诊断看板（权益、回撤、成交、token 盈亏）  
4. 自动化流水线（抓取 -> 回放 -> 刷新看板）  
5. 实盘安全网关检查（默认禁用）
6. AI 策略赛马（多策略生成、排行榜、每策略设定与交易记录）
7. 自动量化引擎（Step0：自动生成策略 -> 自动模拟执行 -> 自动晋级候选）

## 环境准备

1. 确保安装 Python 3.10+。  
2. 复制 `.env.example` 为 `.env`，按需调整配置。

```bash
cp .env.example .env
```

## 真实交易网站（先看这个）

一键启动真实站点（会自动创建 `.venv` 并安装依赖）：

```bash
cd "/Users/chenweibin/Documents/saima polymarkt"
./run_live_site.sh
```

macOS 双击启动按钮文件：

```text
/Users/chenweibin/Documents/saima polymarkt/一键启动.command
```

打开：

```text
http://127.0.0.1:8780
```

页面已安全拆分：

```text
入口页（实时行情 + 路由）: http://127.0.0.1:8780/
模拟盘页（深色仪表盘）: http://127.0.0.1:8780/paper
仪表盘直达: http://127.0.0.1:8780/dashboard
全自动量化页（A/B/C/D 调度）: http://127.0.0.1:8780/quant
模拟盘页（旧版复杂面板）: http://127.0.0.1:8780/paper-legacy
实盘页（可下单）: http://127.0.0.1:8780/live
```

## 仪表盘前端（React + TS + Tailwind）

仪表盘源码目录：

```text
apps/web/dashboard-app
```

构建命令：

```bash
cd "/Users/chenweibin/Documents/saima polymarkt/apps/web/dashboard-app"
npm install --cache .npm-cache
npm run build
```

构建产物会输出到：

```text
apps/web/static/dashboard
```

页面风格：深色主题（TradingView / 3Commas 风格）  
主界面只保留“监控 + 操作”，配置功能（模型接入 / 对话生成策略）已收纳到右上角 `⚙️` 弹窗。

如果你看不到行情，先重新执行一次：

```bash
cd "/Users/chenweibin/Documents/saima polymarkt"
./run_live_site.sh
```

该脚本会自动重启旧的 `8780` 站点进程，避免浏览器还连着旧版本。

要启用真实下单，至少需要在 `.env` 配置：

```bash
LIVE_TRADING_ENABLED=true
LIVE_FORCE_ACK=true
LIVE_MAX_ORDER_USDC=25
POLYMARKET_LIVE_HOST=https://clob.polymarket.com
POLYMARKET_CHAIN_ID=137
POLYMARKET_SIGNATURE_TYPE=0
POLYMARKET_PRIVATE_KEY=你的私钥
POLYMARKET_FUNDER=你的funder地址
POLYMARKET_API_KEY=你的api_key
POLYMARKET_API_SECRET=你的api_secret
POLYMARKET_API_PASSPHRASE=你的api_passphrase
OPENCLAW_ENDPOINT=http://127.0.0.1:8000/generate   # 可选，配置后“生成策略”优先走AI
OPENCLAW_TIMEOUT_SEC=20
NEWS_SEARCH_API_URL=                               # 可选，AI概率评估器会抓新闻摘要
NEWS_SEARCH_API_KEY=
```

建议先把 `LIVE_MAX_ORDER_USDC` 设成非常小（例如 `5`）再试。

## 多模型策略生成（新增）

你现在可以不只用 OpenClaw，也可以接本地 OpenAI 兼容接口（如 Ollama/LM Studio）。

在模拟盘页面：

```text
http://127.0.0.1:8780/paper
```

可用功能：

1. `自动发现本地模型`：会探测常见端口（11434/1234/8000/8788/9000/3000）  
2. `模型分配接口`：可配置多个 provider（`weighted` 或 `priority`）  
3. `注册模型`：手动写入 provider（id/name/company/endpoint/adapter/model/api_key）  
4. `策略生成模块`：按指定 provider 或自动路由生成策略  
5. `公司模型目录`：支持按公司拉取全部模型并下拉选择（openrouter / yunwu / local / custom）

新增（任务化生成）：

1. `策略生成` 已改为可视化任务流（进度条 + 事件日志 + 历史任务）
2. `生成并启动模拟` 可一键串联 Step2 -> Step3
3. 显式 provider 失败不会静默回退模板，会直接报错并显示原因
4. Step3 新增 `赛马排行榜`（按 realized pnl）与 `晋级门禁`（阈值筛选 + 批准候选）
5. Step3 新增 `策略执行事件日志`，可查看 bot 决策与下单事件
6. Step3 新增 `手动下单测试`（限价/市价/撤单/全撤），用于验证策略与交易规则

## `/quant` 全自动量化（A/B/C/D）

`/quant` 页面是独立的 Polymarket 自动量化编排页，包含：

1. 市场数据引擎（A）：Gamma + CLOB 订单簿刷新，支持 WS 增量更新  
2. 信号引擎（B）：套利 / 做市 / AI 概率偏差三策略  
3. 风控引擎（C）：单笔、总敞口、日亏损、连亏降仓、赛马淘汰  
4. 执行引擎（D）：paper/live 执行，订单与成交全量入库  
5. 调度器（Orchestrator）：循环执行或单轮执行

新增硬门禁：

1. 实盘模式默认 `enforce_live_gate=true`  
2. 每策略必须满足 `72h 模拟盘 + PnL > 0 + 胜率 > 45% + fills >= 20` 才允许进入实盘执行  
3. 模拟盘赛马可自动淘汰亏损策略（可配）

量化 API：

```text
GET  /api/quant/status
POST /api/quant/start
POST /api/quant/stop
POST /api/quant/run-once
POST /api/quant/refresh-markets
GET  /api/quant/markets
GET  /api/quant/books
GET  /api/quant/signals
GET  /api/quant/orders
GET  /api/quant/fills
GET  /api/quant/performance
GET  /api/quant/live-gate
GET  /api/quant/events
GET  /api/quant/risk
```

仪表盘 API（轮询 5 秒）：

```text
GET  /api/strategies
GET  /api/pnl/history
GET  /api/trades/recent
GET  /api/account/summary
POST /api/strategy/{id}/start
POST /api/strategy/{id}/stop
```

`adapter` 支持：

1. `openclaw_compatible`：接口形如 `http://127.0.0.1:xxxx/generate`
2. `openai_compatible`：接口形如 `http://127.0.0.1:xxxx/v1/chat/completions`

示例（Ollama OpenAI 兼容）：

```text
provider_id: local-openai
endpoint: http://127.0.0.1:11434/v1/chat/completions
adapter: openai_compatible
model: qwen2.5
```

云模型接入（你自己填 key）：

1. OpenRouter：`company=openrouter`，默认 endpoint `https://openrouter.ai/api/v1/chat/completions`
2. Yunwu：`company=yunwu`，默认 endpoint `https://api.yunwu.ai/v1/chat/completions`
3. 在 `/paper` 的“模型分配接口”里先选公司并填 `API Key`，再点 `拉取该公司全部模型`，选中后写入 provider
4. 返回配置时会脱敏显示 key（`api_key_masked`），不会回显明文

## 实时模拟盘交易（新增，不是空壳）

打开：

```text
http://127.0.0.1:8780/paper
```

你现在可以直接在网页里做这些动作：

1. 手动模拟下单：限价 / 市价 / 撤单 / 全撤  
2. 模拟 Bot 自动跑策略：按策略参数持续下模拟单  
3. 实时查看：
   - 挂单列表
   - 成交记录
   - 每个策略的实时权益、PnL、胜率、手续费
4. 策略赛马闭环：
   - 先用模型生成策略
   - 再让模拟 Bot 跑
   - 观察排行榜盈利后再晋级实盘

新增模拟盘 API（网页已调用）：

```text
GET  /api/paper/models
GET  /api/paper/models/companies
PUT  /api/paper/models
POST /api/paper/models/register
POST /api/paper/models/discover
POST /api/paper/models/catalog
GET  /api/paper/markets
GET  /api/paper/orderbook/{token_id}
GET  /api/paper/trading/status
POST /api/paper/trading/reset
GET  /api/paper/trading/orders
GET  /api/paper/trading/fills
GET  /api/paper/trading/positions
POST /api/paper/trading/orders/limit
POST /api/paper/trading/orders/market
POST /api/paper/trading/orders/cancel/{order_id}
POST /api/paper/trading/orders/cancel-all
POST /api/paper/trading/bot/start
POST /api/paper/trading/bot/stop
GET  /api/paper/trading/bot/status
GET  /api/paper/stream/status
POST /api/paper/stream/start
POST /api/paper/stream/stop
POST /api/paper/stream/subscribe
POST /api/paper/stream/unsubscribe
POST /api/strategies/generate-async
GET  /api/strategies/generate-jobs/{job_id}
GET  /api/strategies/generate-jobs
```

## 自动化自检（Smoke Test）

站点启动后可直接跑端到端冒烟测试（会执行：生成策略任务 -> 启动模拟 Bot -> 校验成交链路 -> 手动下限价单 -> 全撤单 -> 停止 Bot -> 校验自动量化状态）：

```bash
cd "/Users/chenweibin/Documents/saima polymarkt"
.venv/bin/python scripts/workbench_smoke_test.py --base-url http://127.0.0.1:8780
```

如果返回 JSON 里 `ok=true`，说明主流程已打通。

量化调度链路冒烟（`/quant`）：

```bash
cd "/Users/chenweibin/Documents/saima polymarkt"
.venv/bin/python scripts/quant_smoke_test.py --base-url http://127.0.0.1:8780
```

## Step 2（事件驱动）

模拟盘已支持 Polymarket 市场 WebSocket 事件流（book / price_change / tick_size_change）：

1. 默认会把市场 `orderPriceMinTickSize` / `orderMinSize` 同步到本地交易规则缓存  
2. Bot 可开启“优先事件流”模式，优先消费 WS 书本缓存，轮询作为兜底  
3. 前端可看到 stream 状态（running / connected / recv_total / 订阅数）

## 运行

单次抓取快照：

```bash
python apps/trader/run_fetcher.py --once
```

持续抓取：

```bash
python apps/trader/run_fetcher.py --loop --interval-sec 60
```

## 输出文件

- 快照 JSON：`data/raw/polymarket/snapshots/*.json`
- 抓取日志：`data/raw/polymarket/fetch_log.jsonl`

当前抓取程序只读，不会下真实订单。

## 模拟盘回放（Step 2）

使用快照执行模拟盘回放：

```bash
python apps/trader/run_paper_sim.py --max-snapshots 200 --token-limit 3
```

可选策略：

```bash
# 周期策略
python apps/trader/run_paper_sim.py --strategy periodic

# 均值回归策略
python apps/trader/run_paper_sim.py --strategy mean_reversion
```

可选风险阈值：

```bash
python apps/trader/run_paper_sim.py --risk-loss-limit-pct 2.5
```

命令会回放本地快照并生成：

- 汇总报告：`data/raw/polymarket/paper/paper_summary_*.json`
- 成交日志：`data/raw/polymarket/paper/paper_fills_*.jsonl`

当前策略是演示基线，仅用于验证全链路是否打通：  
下单 -> 撮合 -> 资金与持仓更新 -> PnL/权益报告。

## 可视化看板（Step 2 UI）

根据最新模拟盘结果生成本地 HTML 看板：

```bash
python apps/trader/run_paper_viz.py
```

会生成：

- `data/raw/polymarket/paper/report_latest.html`

启动本地网页服务并自动打开浏览器：

```bash
python apps/trader/run_paper_viz.py --serve --open-browser
```

可开启自动刷新（默认 5 秒）：

```bash
python apps/trader/run_paper_viz.py --serve --auto-refresh-sec 5
```

## 自动化流水线（推荐）

一个命令持续运行：抓取行情 -> 模拟回放 -> 更新看板。

```bash
python apps/trader/run_autopilot.py
```

只跑一轮（便于调试）：

```bash
python apps/trader/run_autopilot.py --once
```

开启看板服务：

```bash
python apps/trader/run_paper_viz.py --serve --auto-refresh-sec 5
```

## AI 策略赛马（你当前主需求）

一次性执行策略赛马：

```bash
python apps/trader/run_strategy_race.py --candidates 10 --token-limit 6
```

说明：
- 默认会生成多套策略（模板生成；可接 OpenClaw 接口）
- 每个策略会独立回放并输出：
  - 策略设定参数
  - 指标（PnL、回撤、胜率、成交笔数、是否熔断）
  - 最近成交记录

生成赛马看板：

```bash
python apps/trader/run_race_viz.py --serve --auto-refresh-sec 5
```

打开：

```text
http://127.0.0.1:8770/race_report_latest.html
```

持续自动赛马（实时刷新数据）：

```bash
python apps/trader/run_race_autopilot.py
```

一键同时启动“赛马自动循环 + 看板服务”（推荐）：

```bash
python apps/trader/run_race_stack.py --port 8770
```

打开：

```text
http://127.0.0.1:8770/race_report_latest.html
```

如果你本地 OpenClaw 已提供 HTTP 接口，可直接接入：

```bash
python apps/trader/run_race_autopilot.py --openclaw-endpoint http://127.0.0.1:8000/generate
```

备注：如果 OpenClaw 接口不可用，会自动回退到模板生成，不会中断流程。

## 从赛马到实盘前确认（晋级网关）

筛选满足门槛的策略并输出“候选实盘配置”：

```bash
python apps/trader/run_strategy_gate.py --min-pnl 0 --max-dd-pct 1.5 --min-fills 10 --min-win-rate 0.45
```

输出：

- `data/raw/polymarket/paper/deploy/promotion_candidate.json`

这个文件用于你人工确认后再进入小资金实盘灰度。

## 实盘安全网关

默认 `LIVE_TRADING_ENABLED=false`，不会进入实盘。

检查实盘配置是否满足最低要求：

```bash
python apps/trader/run_live_guard_check.py
```

注意：当前仓库仍是模拟盘主线，未实现真实签名下单执行，严禁直接用于实盘资金。
