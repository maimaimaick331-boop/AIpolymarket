import { useEffect, useMemo, useState } from 'react';
import { Loader2, X } from 'lucide-react';
import { apiGet, apiPost } from '../lib/api';
import type { CatalogModel, CompanyPreset, GenerateJob, ProviderRow, QuantParams } from '../types';

interface SettingsModalProps {
  open: boolean;
  onClose: () => void;
  onRefreshMain: () => Promise<void>;
}

export default function SettingsModal({ open, onClose, onRefreshMain }: SettingsModalProps) {
  const [tab, setTab] = useState<'model' | 'params'>('model');
  const [companies, setCompanies] = useState<CompanyPreset[]>([]);
  const [providers, setProviders] = useState<ProviderRow[]>([]);
  const [catalog, setCatalog] = useState<CatalogModel[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [savingParams, setSavingParams] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [job, setJob] = useState<GenerateJob | null>(null);
  const [jobTimer, setJobTimer] = useState<number | null>(null);
  const [msg, setMsg] = useState('');
  const [quantParams, setQuantParams] = useState<QuantParams | null>(null);

  const [company, setCompany] = useState('yunwu');
  const [endpoint, setEndpoint] = useState('https://api.yunwu.ai/v1/chat/completions');
  const [apiKey, setApiKey] = useState('');
  const [catalogModel, setCatalogModel] = useState('');
  const [providerId, setProviderId] = useState('');
  const [providerName, setProviderName] = useState('');

  const [genProvider, setGenProvider] = useState('');
  const [genCount, setGenCount] = useState(6);
  const [genPrompt, setGenPrompt] = useState('');
  const [genFallback, setGenFallback] = useState(true);

  const companyDefaultMap = useMemo(() => {
    const map: Record<string, CompanyPreset> = {};
    for (const row of companies) {
      map[row.company] = row;
    }
    return map;
  }, [companies]);

  useEffect(() => {
    if (!open) return;
    void loadBase();
    void loadQuantParams();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const preset = companyDefaultMap[company];
    if (!preset) return;
    if (!endpoint.trim()) {
      setEndpoint(preset.default_endpoint || '');
    }
  }, [company, companyDefaultMap, open, endpoint]);

  useEffect(() => {
    return () => {
      if (jobTimer !== null) {
        window.clearInterval(jobTimer);
      }
    };
  }, [jobTimer]);

  async function loadBase() {
    setLoading(true);
    try {
      const [c, p] = await Promise.all([
        apiGet<{ rows: CompanyPreset[] }>('/paper/models/companies'),
        apiGet<{ providers: ProviderRow[] }>('/paper/models'),
      ]);
      setCompanies(c.rows || []);
      setProviders(p.providers || []);
      if (!providerId) {
        const suffix = String(Date.now()).slice(-6);
        setProviderId(`${company}-${suffix}`);
        setProviderName(`${company}-provider`);
      }
      setMsg('配置读取完成');
    } catch (err) {
      setMsg(`读取失败: ${(err as Error).message}`);
    } finally {
      setLoading(false);
    }
  }

  async function loadQuantParams() {
    try {
      const out = await apiGet<QuantParams>('/quant/params');
      setQuantParams(out);
    } catch (err) {
      setMsg(`读取策略参数失败: ${(err as Error).message}`);
    }
  }

  async function fetchCatalog() {
    setLoading(true);
    try {
      const out = await apiPost<{ rows: CatalogModel[] }>('/paper/models/catalog', {
        company,
        endpoint,
        adapter: 'openai_compatible',
        api_key: apiKey,
        extra_headers: {},
        limit: 3000,
      });
      const rows = out.rows || [];
      setCatalog(rows);
      setCatalogModel(rows[0]?.id || '');
      setMsg(`模型目录拉取完成: ${rows.length} 个`);
    } catch (err) {
      setMsg(`拉取失败: ${(err as Error).message}`);
    } finally {
      setLoading(false);
    }
  }

  async function saveProvider() {
    setSaving(true);
    try {
      const pid = providerId.trim() || `${company}-${String(Date.now()).slice(-6)}`;
      await apiPost('/paper/models/register', {
        provider_id: pid,
        name: providerName.trim() || pid,
        endpoint: endpoint.trim(),
        adapter: 'openai_compatible',
        model: catalogModel.trim(),
        company,
        api_key: apiKey.trim(),
        extra_headers: {},
        enabled: true,
        weight: 1,
        priority: 100,
      });
      setGenProvider(pid);
      await loadBase();
      setMsg(`Provider 已保存: ${pid}`);
    } catch (err) {
      setMsg(`保存失败: ${(err as Error).message}`);
    } finally {
      setSaving(false);
    }
  }

  async function generateStrategies() {
    setSaving(true);
    try {
      const out = await apiPost<{ job_id: string; status: string }>('/strategies/generate-async', {
        count: genCount,
        seed: Date.now() % 1000000,
        provider_id: genProvider,
        prompt: genPrompt,
        allow_fallback: genFallback,
      });
      setJob({ job_id: out.job_id, status: out.status, progress_pct: 0, message: '任务已创建' });
      setMsg(`生成任务已创建: ${out.job_id}`);
      if (jobTimer !== null) {
        window.clearInterval(jobTimer);
      }
      const timer = window.setInterval(() => {
        void pollJob(out.job_id);
      }, 1200);
      setJobTimer(timer);
    } catch (err) {
      setMsg(`生成失败: ${(err as Error).message}`);
    } finally {
      setSaving(false);
    }
  }

  async function pollJob(jobId: string) {
    try {
      const row = await apiGet<GenerateJob>(`/strategies/generate-jobs/${encodeURIComponent(jobId)}`);
      setJob(row);
      const status = String(row.status || '').toLowerCase();
      if (['succeeded', 'failed', 'cancelled'].includes(status)) {
        if (jobTimer !== null) {
          window.clearInterval(jobTimer);
          setJobTimer(null);
        }
        await onRefreshMain();
        await loadBase();
      }
    } catch (err) {
      setMsg(`任务轮询失败: ${(err as Error).message}`);
    }
  }

  async function saveQuantParams() {
    if (!quantParams) return;
    setSavingParams(true);
    try {
      const out = await apiPost<{ ok: boolean; params: QuantParams }>('/quant/params', quantParams);
      setQuantParams(out.params);
      setMsg('策略参数已生效');
      await onRefreshMain();
    } catch (err) {
      setMsg(`保存策略参数失败: ${(err as Error).message}`);
    } finally {
      setSavingParams(false);
    }
  }

  async function resetAllData() {
    const confirmed = window.confirm('将清空所有交易记录、信号、PnL、策略与赛马历史，且不可恢复。确定继续吗？');
    if (!confirmed) return;
    const typed = window.prompt('请输入 RESET 以继续');
    if ((typed || '').trim().toUpperCase() !== 'RESET') {
      setMsg('已取消：确认口令不正确');
      return;
    }
    setResetting(true);
    try {
      const out = await apiPost<{ ok: boolean; message?: string; removed_files_count?: number }>('/admin/reset-all-data', {
        confirm: true,
        clear_market_translations: false,
      });
      setCatalog([]);
      setJob(null);
      setMsg(`${out.message || '重置完成'}（清理文件 ${Number(out.removed_files_count || 0)} 个）`);
      await Promise.all([onRefreshMain(), loadBase(), loadQuantParams()]);
    } catch (err) {
      setMsg(`重置失败: ${(err as Error).message}`);
    } finally {
      setResetting(false);
    }
  }

  function setParam<K extends keyof QuantParams>(key: K, value: QuantParams[K]) {
    setQuantParams((prev) => {
      if (!prev) return prev;
      return { ...prev, [key]: value };
    });
  }

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 bg-black/65 backdrop-blur-sm flex items-start justify-center p-8">
      <div className="w-[1050px] card bg-dashboard-card border-dashboard-line max-h-[92vh] overflow-hidden">
        <div className="flex items-center justify-between px-5 py-4 border-b border-dashboard-line">
          <div>
            <div className="text-lg font-semibold">⚙️ 设置中心</div>
            <div className="text-xs text-dashboard-muted mt-1">模型接入 / 对话生成策略 已收纳到此弹窗</div>
            <div className="mt-3 flex items-center gap-2">
              <button
                onClick={() => setTab('model')}
                className={`rounded-lg border px-3 py-1.5 text-sm ${tab === 'model' ? 'border-[#4b5563] bg-[#111827]' : 'border-dashboard-line bg-transparent hover:border-[#4b5563]'}`}
              >
                模型与策略生成
              </button>
              <button
                onClick={() => setTab('params')}
                className={`rounded-lg border px-3 py-1.5 text-sm ${tab === 'params' ? 'border-[#4b5563] bg-[#111827]' : 'border-dashboard-line bg-transparent hover:border-[#4b5563]'}`}
              >
                策略参数
              </button>
            </div>
          </div>
          <button
            onClick={onClose}
            className="rounded-lg border border-dashboard-line bg-[#111827] p-2 hover:border-[#4b5563]"
          >
            <X size={18} />
          </button>
        </div>

        <div className="p-5 space-y-5 overflow-auto max-h-[calc(92vh-78px)] scroll-dark">
          {tab === 'model' ? (
            <>
              <section className="card p-4 space-y-3">
                <div className="font-medium">模型接入</div>
                <div className="grid grid-cols-12 gap-2">
                  <select value={company} onChange={(e) => setCompany(e.target.value)} className="col-span-2 rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2">
                    {(companies || []).map((c) => (
                      <option key={c.company} value={c.company}>{c.company}</option>
                    ))}
                  </select>
                  <input value={endpoint} onChange={(e) => setEndpoint(e.target.value)} placeholder="endpoint" className="col-span-5 rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2" />
                  <input value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="api key" className="col-span-5 rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2" />
                </div>
                <div className="grid grid-cols-12 gap-2">
                  <button onClick={() => void fetchCatalog()} className="col-span-2 rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563]">拉取模型</button>
                  <select value={catalogModel} onChange={(e) => setCatalogModel(e.target.value)} className="col-span-6 rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2">
                    <option value="">选择模型</option>
                    {(catalog || []).map((m) => (
                      <option key={m.id} value={m.id}>{m.id}</option>
                    ))}
                  </select>
                  <input value={providerId} onChange={(e) => setProviderId(e.target.value)} placeholder="provider_id" className="col-span-2 rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2" />
                  <input value={providerName} onChange={(e) => setProviderName(e.target.value)} placeholder="provider 名称" className="col-span-2 rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2" />
                </div>
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => void saveProvider()}
                    disabled={saving}
                    className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563] disabled:opacity-60"
                  >
                    {saving ? <span className="inline-flex items-center gap-1"><Loader2 className="animate-spin" size={14} />保存中</span> : '保存 Provider'}
                  </button>
                  <button onClick={() => void loadBase()} className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563]">刷新列表</button>
                </div>

                <div className="rounded-lg border border-dashboard-line overflow-hidden">
                  <table className="w-full text-sm">
                    <thead className="bg-[#111827] text-dashboard-muted">
                      <tr>
                        <th className="text-left px-3 py-2">provider_id</th>
                        <th className="text-left px-3 py-2">company</th>
                        <th className="text-left px-3 py-2">model</th>
                        <th className="text-left px-3 py-2">状态</th>
                        <th className="text-left px-3 py-2">操作</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(providers || []).slice(0, 30).map((p) => (
                        <tr key={p.provider_id} className="border-t border-dashboard-line">
                          <td className="px-3 py-2 font-mono">{p.provider_id}</td>
                          <td className="px-3 py-2">{p.company}</td>
                          <td className="px-3 py-2">{p.model || '-'}</td>
                          <td className={`px-3 py-2 ${p.enabled ? 'text-dashboard-good' : 'text-yellow-300'}`}>{p.enabled ? '启用' : '禁用'}</td>
                          <td className="px-3 py-2">
                            <button onClick={() => setGenProvider(p.provider_id)} className="rounded border border-dashboard-line px-2 py-1 text-xs hover:border-[#4b5563]">用于策略生成</button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>

              <section className="card p-4 space-y-3">
                <div className="font-medium">对话生成策略</div>
                <div className="grid grid-cols-12 gap-2">
                  <select value={genProvider} onChange={(e) => setGenProvider(e.target.value)} className="col-span-5 rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2">
                    <option value="">自动路由</option>
                    {(providers || []).map((p) => (
                      <option key={p.provider_id} value={p.provider_id}>{p.provider_id} | {p.model || p.company}</option>
                    ))}
                  </select>
                  <input type="number" min={1} max={30} value={genCount} onChange={(e) => setGenCount(Number(e.target.value || 6))} className="col-span-2 rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2" />
                  <label className="col-span-5 inline-flex items-center gap-2 rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 text-sm">
                    <input type="checkbox" checked={genFallback} onChange={(e) => setGenFallback(e.target.checked)} />
                    模型失败时允许模板回退
                  </label>
                </div>
                <textarea value={genPrompt} onChange={(e) => setGenPrompt(e.target.value)} className="w-full rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 h-24" placeholder="输入策略生成要求" />
                <div className="flex items-center gap-2">
                  <button onClick={() => void generateStrategies()} className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563]">发起生成</button>
                  {job ? (
                    <div className="text-xs text-dashboard-muted font-mono">
                      job={job.job_id} | {job.status} | {Number(job.progress_pct || 0)}% | {job.message || job.stage || '-'}
                    </div>
                  ) : null}
                </div>
              </section>
            </>
          ) : null}

          {tab === 'params' ? (
            <section className="card p-4 space-y-4">
              <div className="flex items-center justify-between">
                <div className="font-medium">策略参数（实时生效）</div>
                <div className="flex items-center gap-2">
                  <button onClick={() => void loadQuantParams()} className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563]">刷新参数</button>
                  <button onClick={() => void saveQuantParams()} disabled={savingParams || !quantParams} className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563] disabled:opacity-60">
                    {savingParams ? '保存中...' : '保存并生效'}
                  </button>
                </div>
              </div>

              {quantParams ? (
                <div className="space-y-4 text-sm">
                  <div className="grid grid-cols-3 gap-3">
                    <label className="space-y-1">
                      <div className="text-dashboard-muted">最小 Spread</div>
                      <input type="number" step="0.001" value={quantParams.mm_min_spread} onChange={(e) => setParam('mm_min_spread', Number(e.target.value || 0))} className="w-full rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2" />
                    </label>
                    <label className="space-y-1">
                      <div className="text-dashboard-muted">单市场最大持仓(USDC)</div>
                      <input type="number" step="1" value={quantParams.mm_max_position_per_market_usdc} onChange={(e) => setParam('mm_max_position_per_market_usdc', Number(e.target.value || 0))} className="w-full rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2" />
                    </label>
                    <label className="space-y-1">
                      <div className="text-dashboard-muted">市场最低24h交易量</div>
                      <input type="number" step="10" value={quantParams.mm_min_volume} onChange={(e) => setParam('mm_min_volume', Number(e.target.value || 0))} className="w-full rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2" />
                    </label>
                  </div>

                  <div className="grid grid-cols-3 gap-3">
                    <label className="space-y-1">
                      <div className="text-dashboard-muted">买入套利阈值 (Yes+No&lt;)</div>
                      <input type="number" step="0.001" value={quantParams.arb_buy_threshold} onChange={(e) => setParam('arb_buy_threshold', Number(e.target.value || 0))} className="w-full rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2" />
                    </label>
                    <label className="space-y-1">
                      <div className="text-dashboard-muted">卖出套利阈值 (Yes+No&gt;)</div>
                      <input type="number" step="0.001" value={quantParams.arb_sell_threshold} onChange={(e) => setParam('arb_sell_threshold', Number(e.target.value || 0))} className="w-full rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2" />
                    </label>
                    <label className="space-y-1">
                      <div className="text-dashboard-muted">做市最小深度(USDC)</div>
                      <input type="number" step="10" value={quantParams.mm_min_depth_usdc} onChange={(e) => setParam('mm_min_depth_usdc', Number(e.target.value || 0))} className="w-full rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2" />
                    </label>
                  </div>

                  <div className="grid grid-cols-3 gap-3">
                    <label className="space-y-1">
                      <div className="text-dashboard-muted">AI概率偏差阈值</div>
                      <input type="number" step="0.01" value={quantParams.ai_deviation_threshold} onChange={(e) => setParam('ai_deviation_threshold', Number(e.target.value || 0))} className="w-full rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2" />
                    </label>
                    <label className="space-y-1">
                      <div className="text-dashboard-muted">AI置信度阈值</div>
                      <input type="number" step="0.01" value={quantParams.ai_min_confidence} onChange={(e) => setParam('ai_min_confidence', Number(e.target.value || 0))} className="w-full rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2" />
                    </label>
                    <label className="space-y-1">
                      <div className="text-dashboard-muted">AI评估间隔(分钟)</div>
                      <input type="number" step="1" value={Math.round(quantParams.ai_eval_interval_sec / 60)} onChange={(e) => setParam('ai_eval_interval_sec', Math.max(60, Number(e.target.value || 0) * 60))} className="w-full rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2" />
                    </label>
                  </div>

                  <div className="grid grid-cols-3 gap-3">
                    <label className="inline-flex items-center gap-2 rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2">
                      <input type="checkbox" checked={quantParams.enable_arb} onChange={(e) => setParam('enable_arb', e.target.checked)} />
                      启用套利策略
                    </label>
                    <label className="inline-flex items-center gap-2 rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2">
                      <input type="checkbox" checked={quantParams.enable_mm} onChange={(e) => setParam('enable_mm', e.target.checked)} />
                      启用做市策略
                    </label>
                    <label className="inline-flex items-center gap-2 rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2">
                      <input type="checkbox" checked={quantParams.enable_ai} onChange={(e) => setParam('enable_ai', e.target.checked)} />
                      启用AI策略
                    </label>
                  </div>

                  <div className="rounded-xl border border-red-500/60 bg-red-950/20 p-4 space-y-2">
                    <div className="text-red-300 font-medium">危险操作：重置所有数据</div>
                    <div className="text-xs text-red-200/90">
                      清空交易流水、策略信号、PnL、策略列表、赛马文件。模型 Provider 与 API Key 配置会保留。
                    </div>
                    <button
                      onClick={() => void resetAllData()}
                      disabled={resetting}
                      className="rounded-lg border border-red-400/70 bg-red-900/40 px-3 py-2 text-red-100 hover:bg-red-900/60 disabled:opacity-60"
                    >
                      {resetting ? '重置中...' : '重置所有数据（不可恢复）'}
                    </button>
                  </div>
                </div>
              ) : (
                <div className="text-sm text-dashboard-muted">策略参数加载中...</div>
              )}
            </section>
          ) : null}

          <div className="text-xs text-dashboard-muted">
            {loading ? '加载中...' : msg}
          </div>
        </div>
      </div>
    </div>
  );
}
