# Polymarket API 接入与产品对齐审计（2026-03-05）

## 1. 本地实测结论

测试时间（UTC）：2026-03-04T19:19:55Z  
服务地址：`http://127.0.0.1:8780`

- `GET /api/paper/markets?limit=1`：返回 `source=gamma`，且包含 `outcomes`（Yes/No + token_id + price）。
- `GET /api/paper/orderbook/{token_id}`：返回排序后的 `bids/asks`，买一卖一价格可用于模拟盘决策。
- `POST /api/paper/trading/orders/market`：可成交并写入 fills（模拟盘真实下单链路可用）。
- `POST /api/strategies/generate`（显式 provider=`yunwu-80033`）：成功返回 `source=yunwu`、`used_fallback=false`。
- 显式 provider 失败时：返回 `502`，不再静默回退模板（避免“假成功”）。

## 2. 与 Polymarket 产品逻辑对齐情况

### 已对齐

- 行情来源：`Gamma markets` + `CLOB orderbook`（市场与盘口分离）。
- 交易对象：按 outcome token（Yes/No token）交易，而不是仅 market 粒度。
- 模拟交易：支持下单、成交、订单状态、成交记录、策略收益统计。
- 实盘风控门禁：`LIVE_TRADING_ENABLED` + `confirm_live` + 单笔限额。

### 部分对齐

- 实盘账户视图：有 `open-orders / trades / balance` 接口，但需配置真实 API 凭证后才能完整验证。
- WebSocket 行情流：后端已接入市场流与订阅控制，UI 仍可继续增强“每个策略/每个 token 实时可见性”。

### 未对齐（产品化仍需补齐）

- 与 Polymarket 前台一致的完整交易面板（深度档位、下单滑点估算、成交回报细节）仍需增强。
- 资金侧流程（充值/桥接/钱包资产分层）未纳入当前产品范围。
- 事件结算后的持仓归档、复盘归因报告还不完整。

## 3. 本次修复（关键）

- 修复“模型失败静默回退模板”：
  - 显式 provider 调用失败/空返回 -> 直接报错；
  - 自动路由模式才允许模板兜底，并返回 `used_fallback` 标记。
- 市场数据补充 outcome 映射：
  - `/api/paper/markets` 新增 `outcomes`，前端可按 Yes/No token 选择。
- 盘口统一排序：
  - `bids` 按价格降序，`asks` 按价格升序，避免 UI 显示错档。

## 4. 参考文档

- Polymarket API Introduction: https://docs.polymarket.com/api-reference/introduction
- Gamma Markets endpoint: https://docs.polymarket.com/developers/gamma-markets-api/get-markets
- CLOB Get Order Book: https://docs.polymarket.com/developers/CLOB/prices-books/get-order-book-summary
