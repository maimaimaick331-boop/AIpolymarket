# 可视化产品对标笔记（2026-03-05）

目标：把“模型生成策略 -> 模拟验证 -> 实盘接入”做成可观测、可追踪、可复盘的产品流，而不是按钮集合。

## 参考页面

1. Hummingbot Dashboard  
   - https://hummingbot.org/dashboard  
   - 借鉴点：运行状态面板、策略实例状态、日志可回看、可控启停。
2. QuantConnect Algorithm Lab  
   - https://www.quantconnect.com/docs/v2/cloud-platform/projects/getting-started  
   - 借鉴点：任务化（回测/部署）与结果分离，任务状态明确。
3. Freqtrade 文档（Webserver/UI）  
   - https://www.freqtrade.io/en/stable/  
   - 借鉴点：策略/交易记录/运行控制分层，不混在同一块。
4. Polymarket API 文档  
   - https://docs.polymarket.com/api-reference/introduction  
   - 借鉴点：市场对象与 outcome token 对象分离，盘口以 token 为核心。

## 落地到当前产品的改造

1. 任务化策略生成（新增 async job）
   - `POST /api/strategies/generate-async`
   - `GET /api/strategies/generate-jobs/{job_id}`
   - `GET /api/strategies/generate-jobs`
2. 可见进度
   - 页面显示阶段、进度条、任务事件流（queued/running/succeeded/failed）。
3. 串联动作
   - 新增“生成并启动模拟”，任务成功后自动跳转并启动 Step 3。
4. 流程状态总览
   - 顶部增加 workflow 状态卡（模型接入、策略生成、模拟执行、盈利评估）。
5. 交易对象对齐
   - `paper markets` 返回 `outcomes`，前端按 outcome token（Yes/No）选择交易对象。
