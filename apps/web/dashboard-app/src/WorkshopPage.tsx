import { useEffect, useMemo, useRef, useState } from 'react';
import { ArrowLeft, Bot, Plus, RefreshCw, Send, Trash2 } from 'lucide-react';
import { apiGet, apiPost } from './lib/api';
import type { ProviderPoolState, ProviderRow, WorkshopChatResponse, WorkshopDraft, WorkshopMessage } from './types';

const EMPTY_DRAFT: WorkshopDraft = {
  name: '价格回归捕捉',
  description: '在目标市场的价格/价差达到阈值时执行交易。',
  type: 'spread_capture',
  direction: 'both',
  trigger_conditions: [
    { type: 'spread_threshold', operator: '>=', value: 0.05, description: 'spread 至少 5%' },
    { type: 'volume_filter', operator: '>=', value: 1000, description: '24h 成交额至少 1000 USDC' },
  ],
  position_sizing: { per_trade_usdc: 15, max_total_usdc: 150 },
  risk_management: { stop_loss_total: -40, stop_loss_per_trade_pct: -0.08, take_profit_per_trade_pct: 0.1, max_consecutive_losses: 5 },
  market_filter: { min_volume_24h: 1000, min_liquidity: 500, keywords: 'all' },
  check_interval_minutes: 15,
};

interface WorkshopProviderResp {
  mode: string;
  count: number;
  providers: ProviderRow[];
  selected_provider_id: string;
  provider_pool?: ProviderPoolState;
}

interface WorkshopDeployResp {
  ok: boolean;
  strategy_id: string;
  detail_url: string;
  message: string;
}

function extractStrategyJsonBlock(text: string): WorkshopDraft | null {
  const raw = String(text || '');
  const marker = '```strategy_json';
  const start = raw.indexOf(marker);
  if (start < 0) return null;
  const tail = raw.slice(start + marker.length);
  const end = tail.indexOf('```');
  if (end < 0) return null;
  const body = tail.slice(0, end).trim();
  if (!body) return null;
  try {
    const parsed = JSON.parse(body);
    if (parsed && typeof parsed === 'object') return parsed as WorkshopDraft;
  } catch {
    return null;
  }
  return null;
}

export default function WorkshopPage() {
  const [providers, setProviders] = useState<ProviderRow[]>([]);
  const [providerId, setProviderId] = useState('');
  const [messages, setMessages] = useState<WorkshopMessage[]>([
    {
      role: 'assistant',
      content: '请输入策略想法，我会实时生成结构化策略卡片。确认后点击“部署到模拟盘”。',
    },
  ]);
  const [input, setInput] = useState('');
  const [draft, setDraft] = useState<WorkshopDraft>(EMPTY_DRAFT);
  const [sending, setSending] = useState(false);
  const [deploying, setDeploying] = useState(false);
  const [status, setStatus] = useState('');
  const [error, setError] = useState('');
  const chatBottomRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);

  const providerRows = useMemo(() => providers.filter((x) => x.enabled && (x.available ?? true)), [providers]);

  async function loadProviders() {
    try {
      const out = await apiGet<WorkshopProviderResp>('/workshop/providers');
      const rows = out.providers || [];
      setProviders(rows);
      if (out.selected_provider_id && rows.some((r) => r.provider_id === out.selected_provider_id && r.enabled)) {
        setProviderId(out.selected_provider_id);
      } else if (rows.length > 0) {
        const firstEnabled = rows.find((x) => x.enabled && (x.available ?? true));
        setProviderId(firstEnabled?.provider_id || rows[0].provider_id);
      }
      setError('');
    } catch (err) {
      setError(`读取模型列表失败: ${(err as Error).message}`);
    }
  }

  useEffect(() => {
    void loadProviders();
  }, []);

  useEffect(() => {
    chatBottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [messages, sending]);

  async function sendMessage() {
    const text = input.trim();
    if (!text || sending) return;
    const nextMessages: WorkshopMessage[] = [...messages, { role: 'user', content: text }];
    setMessages(nextMessages);
    setInput('');
    setSending(true);
    setError('');
    try {
      const out = await apiPost<WorkshopChatResponse>('/workshop/chat', {
        provider_id: providerId,
        messages: nextMessages,
        draft,
      });
      const parsedDraft = extractStrategyJsonBlock(out.assistant || '');
      if (parsedDraft) {
        setDraft({ ...(out.draft || draft), ...parsedDraft });
      } else {
        setDraft(out.draft);
        if ((out.source || '').startsWith('llm:') || out.format_error) {
          setError('模型返回中缺少 strategy_json 代码块或 JSON 不可解析');
        }
      }
      const switched =
        Boolean(out.fallback_to_provider_id) ||
        (Boolean(out.selected_provider_id) && Boolean(out.provider_id) && out.selected_provider_id !== out.provider_id);
      const assistantRows: WorkshopMessage[] = [];
      if (switched) {
        const fromId = out.fallback_from_provider_id || out.selected_provider_id || providerId || '当前模型';
        const toId = out.fallback_to_provider_id || out.provider_id || '模板回退';
        assistantRows.push({
          role: 'assistant',
          content: `⚠️ 当前模型不可用，正在切换到 ${toId}（原模型: ${fromId}）`,
        });
        if (out.fallback_to_provider_id) {
          setProviderId(out.fallback_to_provider_id);
        }
      }
      assistantRows.push({ role: 'assistant', content: out.assistant || '策略草案已更新。' });
      setMessages((prev) => [...prev, ...assistantRows]);
      const statusRows = [
        `source=${out.source}`,
        out.provider_id ? `provider=${out.provider_id}` : 'provider=local_fallback',
        out.fallback_reason ? `fallback=${out.fallback_reason}` : '',
        out.llm_error ? `llm_error=${out.llm_error}` : '',
      ].filter(Boolean);
      setStatus(statusRows.join(' | '));
    } catch (err) {
      const msg = (err as Error).message || '对话失败';
      setMessages((prev) => [...prev, { role: 'assistant', content: `对话失败: ${msg}` }]);
      setError(msg);
    } finally {
      setSending(false);
    }
  }

  async function deployStrategy() {
    if (deploying) return;
    setDeploying(true);
    setError('');
    try {
      const out = await apiPost<WorkshopDeployResp>('/workshop/deploy', {
        provider_id: providerId,
        draft,
      });
      if (out.detail_url) {
        window.location.href = out.detail_url;
        return;
      }
      window.location.href = `/strategy/${encodeURIComponent(out.strategy_id)}`;
    } catch (err) {
      setError(`部署失败: ${(err as Error).message}`);
    } finally {
      setDeploying(false);
    }
  }

  function updateDraft<K extends keyof WorkshopDraft>(key: K, value: WorkshopDraft[K]) {
    setDraft((prev) => ({ ...prev, [key]: value }));
  }

  function updateTrigger(index: number, field: 'type' | 'operator' | 'value' | 'description', value: string) {
    setDraft((prev) => {
      const rows = [...prev.trigger_conditions];
      const row = { ...rows[index] };
      if (field === 'value') {
        const n = Number(value);
        row.value = Number.isFinite(n) ? n : value;
      } else {
        // Keep condition rows editable inline for quick iteration.
        row[field] = value;
      }
      rows[index] = row;
      return { ...prev, trigger_conditions: rows };
    });
  }

  function addTrigger() {
    setDraft((prev) => ({
      ...prev,
      trigger_conditions: [
        ...prev.trigger_conditions,
        { type: 'spread_threshold', operator: '>=', value: 0.05, description: '新条件' },
      ],
    }));
  }

  function removeTrigger(index: number) {
    setDraft((prev) => {
      const rows = prev.trigger_conditions.filter((_, i) => i !== index);
      return { ...prev, trigger_conditions: rows.length > 0 ? rows : prev.trigger_conditions };
    });
  }

  function renderMessage(row: WorkshopMessage, idx: number) {
    const right = row.role === 'user';
    return (
      <div key={`${row.role}-${idx}`} className={`flex ${right ? 'justify-end' : 'justify-start'}`}>
        <div
          className={`max-w-[88%] rounded-xl border px-3 py-2 whitespace-pre-wrap text-sm ${
            right
              ? 'border-blue-400/40 bg-blue-400/10'
              : 'border-dashboard-line bg-[#111827]'
          }`}
        >
          <div className="text-[11px] text-dashboard-muted mb-1">{right ? '你' : 'AI'}</div>
          {row.content}
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-dashboard-bg text-dashboard-text px-5 py-4 space-y-4">
      <header className="card px-4 py-3 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold m-0">策略工坊</h1>
          <div className="text-xs text-dashboard-muted mt-1">对话生成 → 预览确认 → 部署到模拟盘</div>
        </div>
        <div className="flex items-center gap-2">
          <a href="/dashboard" className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563] inline-flex items-center gap-2">
            <ArrowLeft size={14} />
            仪表盘
          </a>
          <a
            href="/live"
            className="rounded-lg border border-orange-500/50 bg-[#111827] px-3 py-2 hover:border-orange-400 inline-flex items-center gap-2 text-orange-300"
          >
            🔴 实盘中心
          </a>
          <a href="/strategies" className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563]">策略管理</a>
          <a href="/history" className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563]">历史</a>
          <a href="/settings" className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563]">设置</a>
          <button
            onClick={() => void loadProviders()}
            className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563] inline-flex items-center gap-2"
          >
            <RefreshCw size={14} />
            刷新模型
          </button>
        </div>
      </header>

      <section className="grid grid-cols-5 gap-4">
        <div className="col-span-3 card p-4 flex flex-col h-[calc(100vh-130px)] min-h-[760px]">
          <div className="flex items-center gap-2 mb-3">
            <Bot size={16} className="text-sky-300" />
            <span className="text-sm text-dashboard-muted">AI 对话</span>
            <select
              value={providerId}
              onChange={(e) => setProviderId(e.target.value)}
              className="ml-auto rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 text-sm"
            >
              {providerRows.length === 0 ? <option value="">暂无可用 provider</option> : null}
              {providerRows.map((p) => (
                <option key={p.provider_id} value={p.provider_id}>
                  {p.provider_id} | {p.model || p.company}
                </option>
              ))}
            </select>
          </div>

          <div className="flex-1 overflow-auto scroll-dark border border-dashboard-line rounded-xl bg-[#0f172a] p-3 space-y-3">
            {messages.map(renderMessage)}
            {sending ? <div className="text-xs text-dashboard-muted">AI 正在生成策略...</div> : null}
            <div ref={chatBottomRef} />
          </div>

          <div className="mt-3 grid grid-cols-12 gap-2">
            <textarea
              ref={inputRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="例如：我想做低回撤、优先高流动市场，单笔不超过20USDC，亏损超过1.5%暂停。"
              className="col-span-10 h-24 rounded-xl border border-dashboard-line bg-[#111827] px-3 py-2 resize-none"
            />
            <button
              onClick={() => void sendMessage()}
              disabled={sending}
              className="col-span-2 rounded-xl border border-dashboard-line bg-[#111827] hover:border-[#4b5563] disabled:opacity-60 inline-flex items-center justify-center gap-2"
            >
              <Send size={14} />
              发送
            </button>
          </div>
        </div>

        <div className="col-span-2 card p-4 h-[calc(100vh-130px)] min-h-[760px] overflow-auto scroll-dark space-y-3">
          <div className="flex items-center justify-between">
            <div className="text-sm text-dashboard-muted">策略预览卡片</div>
            <div className="text-xs text-dashboard-muted">{draft.type}</div>
          </div>

          <label className="block text-xs text-dashboard-muted">
            名称
            <input
              value={draft.name}
              onChange={(e) => updateDraft('name', e.target.value)}
              className="mt-1 w-full rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 text-sm"
            />
          </label>

          <label className="block text-xs text-dashboard-muted">
            描述
            <textarea
              value={draft.description}
              onChange={(e) => updateDraft('description', e.target.value)}
              className="mt-1 w-full h-20 rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 text-sm resize-none"
            />
          </label>

          <div className="grid grid-cols-2 gap-2">
            <label className="block text-xs text-dashboard-muted">
              类型
              <select
                value={draft.type}
                onChange={(e) => updateDraft('type', e.target.value)}
                className="mt-1 w-full rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 text-sm"
              >
                <option value="ai_probability">ai_probability</option>
                <option value="arbitrage">arbitrage</option>
                <option value="market_making">market_making</option>
                <option value="spread_capture">spread_capture</option>
                <option value="custom">custom</option>
              </select>
            </label>
            <label className="block text-xs text-dashboard-muted">
              方向
              <select
                value={draft.direction}
                onChange={(e) => updateDraft('direction', e.target.value)}
                className="mt-1 w-full rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 text-sm"
              >
                <option value="buy_yes">buy_yes（买Yes）</option>
                <option value="buy_no">buy_no（买No）</option>
                <option value="both">both（双向）</option>
                <option value="market_make">market_make（做市）</option>
              </select>
            </label>
          </div>

          <section className="rounded-xl border border-dashboard-line p-3 space-y-2">
            <div className="flex items-center justify-between">
              <div className="text-xs text-dashboard-muted">触发条件</div>
              <button onClick={addTrigger} className="rounded border border-dashboard-line px-2 py-1 text-xs hover:border-[#4b5563] inline-flex items-center gap-1">
                <Plus size={12} />
                新增
              </button>
            </div>
            {draft.trigger_conditions.map((row, idx) => (
              <div key={`${row.type}-${idx}`} className="grid grid-cols-12 gap-2">
                <select
                  value={row.type}
                  onChange={(e) => updateTrigger(idx, 'type', e.target.value)}
                  className="col-span-3 rounded-lg border border-dashboard-line bg-[#111827] px-2 py-1.5 text-xs"
                >
                  <option value="spread_threshold">spread_threshold</option>
                  <option value="ai_deviation">ai_deviation</option>
                  <option value="arb_gap">arb_gap</option>
                  <option value="volume_filter">volume_filter</option>
                  <option value="price_range">price_range</option>
                </select>
                <select
                  value={row.operator}
                  onChange={(e) => updateTrigger(idx, 'operator', e.target.value)}
                  className="col-span-2 rounded-lg border border-dashboard-line bg-[#111827] px-2 py-1.5 text-xs"
                >
                  <option value=">=">&gt;=</option>
                  <option value="<=">&lt;=</option>
                  <option value=">">&gt;</option>
                  <option value="<">&lt;</option>
                  <option value="==">==</option>
                </select>
                <input
                  value={String(row.value)}
                  onChange={(e) => updateTrigger(idx, 'value', e.target.value)}
                  className="col-span-2 rounded-lg border border-dashboard-line bg-[#111827] px-2 py-1.5 text-xs"
                />
                <input
                  value={row.description}
                  onChange={(e) => updateTrigger(idx, 'description', e.target.value)}
                  className="col-span-4 rounded-lg border border-dashboard-line bg-[#111827] px-2 py-1.5 text-xs"
                />
                <button
                  onClick={() => removeTrigger(idx)}
                  className="col-span-1 rounded border border-dashboard-line hover:border-[#4b5563] inline-flex items-center justify-center"
                >
                  <Trash2 size={12} />
                </button>
              </div>
            ))}
          </section>

          <div className="grid grid-cols-2 gap-2">
            <label className="block text-xs text-dashboard-muted">
              单笔仓位
              <input
                type="number"
                value={draft.position_sizing.per_trade_usdc}
                onChange={(e) =>
                  updateDraft('position_sizing', {
                    ...draft.position_sizing,
                    per_trade_usdc: Number(e.target.value || 0),
                  })
                }
                className="mt-1 w-full rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 text-sm"
              />
            </label>
            <label className="block text-xs text-dashboard-muted">
              总持仓上限
              <input
                type="number"
                value={draft.position_sizing.max_total_usdc}
                onChange={(e) =>
                  updateDraft('position_sizing', {
                    ...draft.position_sizing,
                    max_total_usdc: Number(e.target.value || 0),
                  })
                }
                className="mt-1 w-full rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 text-sm"
              />
            </label>
          </div>

          <div className="grid grid-cols-4 gap-2">
            <label className="block text-xs text-dashboard-muted">
              总止损
              <input
                type="number"
                value={draft.risk_management.stop_loss_total}
                onChange={(e) =>
                  updateDraft('risk_management', {
                    ...draft.risk_management,
                    stop_loss_total: Number(e.target.value || 0),
                  })
                }
                className="mt-1 w-full rounded-lg border border-dashboard-line bg-[#111827] px-2 py-2 text-sm"
              />
            </label>
            <label className="block text-xs text-dashboard-muted">
              单笔止损%
              <input
                type="number"
                step="0.01"
                value={draft.risk_management.stop_loss_per_trade_pct}
                onChange={(e) =>
                  updateDraft('risk_management', {
                    ...draft.risk_management,
                    stop_loss_per_trade_pct: Number(e.target.value || 0),
                  })
                }
                className="mt-1 w-full rounded-lg border border-dashboard-line bg-[#111827] px-2 py-2 text-sm"
              />
            </label>
            <label className="block text-xs text-dashboard-muted">
              单笔止盈%
              <input
                type="number"
                step="0.01"
                value={draft.risk_management.take_profit_per_trade_pct}
                onChange={(e) =>
                  updateDraft('risk_management', {
                    ...draft.risk_management,
                    take_profit_per_trade_pct: Number(e.target.value || 0),
                  })
                }
                className="mt-1 w-full rounded-lg border border-dashboard-line bg-[#111827] px-2 py-2 text-sm"
              />
            </label>
            <label className="block text-xs text-dashboard-muted">
              最大连亏
              <input
                type="number"
                min={1}
                value={draft.risk_management.max_consecutive_losses}
                onChange={(e) =>
                  updateDraft('risk_management', {
                    ...draft.risk_management,
                    max_consecutive_losses: Number(e.target.value || 1),
                  })
                }
                className="mt-1 w-full rounded-lg border border-dashboard-line bg-[#111827] px-2 py-2 text-sm"
              />
            </label>
          </div>

          <div className="grid grid-cols-3 gap-2">
            <label className="block text-xs text-dashboard-muted">
              检查间隔(分钟)
              <input
                type="number"
                min={1}
                value={draft.check_interval_minutes}
                onChange={(e) => updateDraft('check_interval_minutes', Number(e.target.value || 1))}
                className="mt-1 w-full rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 text-sm"
              />
            </label>
            <label className="block text-xs text-dashboard-muted">
              最低24h成交额
              <input
                type="number"
                value={draft.market_filter.min_volume_24h}
                onChange={(e) =>
                  updateDraft('market_filter', {
                    ...draft.market_filter,
                    min_volume_24h: Number(e.target.value || 0),
                  })
                }
                className="mt-1 w-full rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 text-sm"
              />
            </label>
            <label className="block text-xs text-dashboard-muted">
              最低流动性
              <input
                type="number"
                value={draft.market_filter.min_liquidity}
                onChange={(e) =>
                  updateDraft('market_filter', {
                    ...draft.market_filter,
                    min_liquidity: Number(e.target.value || 0),
                  })
                }
                className="mt-1 w-full rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 text-sm"
              />
            </label>
          </div>

          <label className="block text-xs text-dashboard-muted">
            关键词（逗号分隔，留空=all）
            <input
              value={Array.isArray(draft.market_filter.keywords) ? draft.market_filter.keywords.join(',') : String(draft.market_filter.keywords)}
              onChange={(e) =>
                updateDraft('market_filter', {
                  ...draft.market_filter,
                  keywords:
                    e.target.value.trim() === ''
                      ? 'all'
                      : e.target.value
                          .split(',')
                          .map((x) => x.trim())
                          .filter(Boolean),
                })
              }
              className="mt-1 w-full rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 text-sm"
            />
          </label>

          <div className="pt-2 flex items-center gap-2">
            <button
              onClick={() => inputRef.current?.focus()}
              className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563]"
            >
              继续对话修改
            </button>
            <button
              onClick={() => void deployStrategy()}
              disabled={deploying}
              className="rounded-lg border border-dashboard-line bg-emerald-500/15 text-dashboard-good px-3 py-2 hover:border-emerald-400/70 disabled:opacity-60"
            >
              {deploying ? '部署中...' : '部署到模拟盘'}
            </button>
          </div>

          {status ? <div className="text-xs text-dashboard-muted">{status}</div> : null}
          {error ? <div className="text-xs text-dashboard-bad">{error}</div> : null}
        </div>
      </section>
    </div>
  );
}
