import { useCallback, useEffect, useMemo, useState } from 'react';
import { Bot, Eye, EyeOff, Key, RefreshCw, Save, Shield } from 'lucide-react';
import { apiGet, apiPost, toLocalTime } from './lib/api';
import type {
  BotStatus,
  LiveGateResponse,
  LiveGateRow,
  LiveOrderRow,
  LiveStatus,
  LiveTradeRow,
  MarketMonitorRow,
  StrategyOverviewResponse,
  StrategyRow,
  StrategyTradeRow,
} from './types';

type TabKey = 'tab1' | 'tab2' | 'tab3';

type NoticeTone = 'ok' | 'bad';

interface ApiDisabled {
  ok?: boolean;
  disabled?: boolean;
  reason?: string;
}

interface LiveHealthResult {
  ok?: boolean;
  disabled?: boolean;
  reason?: string;
  error?: string;
  balance?: unknown;
  rows?: unknown[];
}

interface LiveCredentialStatus {
  live_trading_enabled: boolean;
  live_force_ack: boolean;
  live_max_order_usdc: number;
  live_host: string;
  chain_id: number;
  signature_type: number;
  has_private_key: boolean;
  private_key_masked: string;
  has_funder: boolean;
  funder_masked: string;
  has_api_key: boolean;
  api_key_masked: string;
  has_api_secret: boolean;
  api_secret_masked: string;
  has_api_passphrase: boolean;
  api_passphrase_masked: string;
}

interface CredentialForm {
  live_trading_enabled: boolean;
  live_force_ack: boolean;
  live_max_order_usdc: number;
  live_host: string;
  chain_id: number;
  signature_type: number;
  private_key: string;
  funder: string;
  api_key: string;
  api_secret: string;
  api_passphrase: string;
}

type MarketSortKey = 'volume' | 'spread' | 'depth';

type MarketBrowserRow = MarketMonitorRow & {
  yes_mid?: number;
  no_mid?: number;
};

interface PaperOutcomeRow {
  token_id?: string;
  outcome?: string;
  price?: number | null;
}

interface PaperMarketRow {
  id?: string;
  outcomes?: PaperOutcomeRow[];
  token_ids?: string[];
}

interface TokenOption {
  token_id: string;
  label: string;
  price?: number | null;
}

type GateThresholds = LiveGateResponse['thresholds'];

const DEFAULT_GATE_THRESHOLDS: GateThresholds = {
  min_hours: 72,
  min_win_rate: 0.45,
  min_pnl: 0,
  min_fills: 20,
};

function asNumber(v: unknown, fallback = 0): number {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

function fmt4(v: unknown): string {
  return asNumber(v, 0).toFixed(4);
}

function fmtAmount(v: unknown): string {
  return asNumber(v, 0).toLocaleString('zh-CN', {
    minimumFractionDigits: 4,
    maximumFractionDigits: 4,
  });
}

function statusClass(status: string): string {
  const s = String(status || '').toLowerCase();
  if (s === 'running') return 'text-dashboard-good';
  if (s === 'paused') return 'text-yellow-300';
  return 'text-dashboard-muted';
}

function extractRows<T extends LiveOrderRow | LiveTradeRow>(payload: unknown): T[] {
  if (Array.isArray(payload)) return payload as T[];
  if (!payload || typeof payload !== 'object') return [];
  const obj = payload as { rows?: unknown; data?: unknown };
  if (Array.isArray(obj.rows)) return obj.rows as T[];
  if (Array.isArray(obj.data)) return obj.data as T[];
  return [];
}

export default function LiveTradingPage() {
  const [activeTab, setActiveTab] = useState<TabKey>('tab2');

  const [strategies, setStrategies] = useState<StrategyRow[]>([]);
  const [gateRows, setGateRows] = useState<LiveGateRow[]>([]);
  const [gatePassCount, setGatePassCount] = useState(0);
  const [gateTotalCount, setGateTotalCount] = useState(0);
  const [gateThresholds, setGateThresholds] = useState<GateThresholds>(DEFAULT_GATE_THRESHOLDS);
  const [liveStatus, setLiveStatus] = useState<LiveStatus | null>(null);
  const [balanceResult, setBalanceResult] = useState<LiveHealthResult | null>(null);
  const [ordersResult, setOrdersResult] = useState<LiveHealthResult | null>(null);
  const [tradesResult, setTradesResult] = useState<LiveHealthResult | null>(null);
  const [checking, setChecking] = useState(false);
  const [selfCheckAt, setSelfCheckAt] = useState('');
  const [guideOpen, setGuideOpen] = useState<boolean>(false);
  const [credLoading, setCredLoading] = useState(false);
  const [credSaving, setCredSaving] = useState(false);
  const [credSaved, setCredSaved] = useState(false);
  const [credError, setCredError] = useState('');
  const [credStatus, setCredStatus] = useState<LiveCredentialStatus | null>(null);
  const [credRestartHint, setCredRestartHint] = useState<{ message: string; command: string; note: string } | null>(null);
  const [credForm, setCredForm] = useState<CredentialForm>({
    live_trading_enabled: false,
    live_force_ack: true,
    live_max_order_usdc: 25,
    live_host: 'https://clob.polymarket.com',
    chain_id: 137,
    signature_type: 0,
    private_key: '',
    funder: '',
    api_key: '',
    api_secret: '',
    api_passphrase: '',
  });
  const [showPK, setShowPK] = useState(false);
  const [showSecret, setShowSecret] = useState(false);
  const [showPass, setShowPass] = useState(false);
  const [showApiKey, setShowApiKey] = useState(false);

  const [selectedStrategyId, setSelectedStrategyId] = useState('');
  const [strategyDetail, setStrategyDetail] = useState<StrategyOverviewResponse | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState('');
  const [showProfitableOnly, setShowProfitableOnly] = useState(false);
  const [strategyTrades, setStrategyTrades] = useState<StrategyTradeRow[]>([]);
  const [tokenId, setTokenId] = useState('');
  const [side, setSide] = useState<'BUY' | 'SELL'>('BUY');
  const [orderType, setOrderType] = useState<'GTC' | 'FOK'>('GTC');
  const [price, setPrice] = useState<number>(0.5);
  const [size, setSize] = useState<number>(10);
  const [amount, setAmount] = useState<number>(20);
  const [confirmLive, setConfirmLive] = useState(false);

  const [botTokenId, setBotTokenId] = useState('');
  const [botInterval, setBotInterval] = useState<number>(20);
  const [botStatus, setBotStatus] = useState<BotStatus | null>(null);

  const [openOrders, setOpenOrders] = useState<LiveOrderRow[]>([]);
  const [recentTrades, setRecentTrades] = useState<LiveTradeRow[]>([]);
  const [openOrdersDisabled, setOpenOrdersDisabled] = useState<ApiDisabled | null>(null);
  const [recentTradesDisabled, setRecentTradesDisabled] = useState<ApiDisabled | null>(null);

  const [resultText, setResultText] = useState('');
  const [resultTone, setResultTone] = useState<NoticeTone>('ok');
  const [busyOrder, setBusyOrder] = useState(false);
  const [busyBot, setBusyBot] = useState(false);
  const [refreshAt, setRefreshAt] = useState('');

  const [marketRows, setMarketRows] = useState<MarketBrowserRow[]>([]);
  const [marketSearch, setMarketSearch] = useState('');
  const [marketSort, setMarketSort] = useState<MarketSortKey>('volume');
  const [marketPage, setMarketPage] = useState(1);
  const [marketLoading, setMarketLoading] = useState(false);
  const [marketError, setMarketError] = useState('');
  const [expandedMarketId, setExpandedMarketId] = useState('');
  const [marketTokenLoadingId, setMarketTokenLoadingId] = useState('');
  const [marketTokenError, setMarketTokenError] = useState<Record<string, string>>({});
  const [tokenOptionsByMarket, setTokenOptionsByMarket] = useState<Record<string, TokenOption[]>>({});

  const strategyGateMap = useMemo(() => {
    const m = new Map<string, LiveGateRow>();
    for (const row of gateRows) {
      m.set(String(row.strategy_id || ''), row);
    }
    return m;
  }, [gateRows]);

  const filteredStrategies = useMemo(() => {
    let rows = [...strategies];
    if (showProfitableOnly) {
      rows = rows.filter((row) => asNumber(row.total_pnl, 0) > 0);
    }
    rows.sort((a, b) => asNumber(b.total_pnl, 0) - asNumber(a.total_pnl, 0));
    return rows;
  }, [strategies, showProfitableOnly]);

  const selectedStrategy = useMemo(
    () => strategies.find((row) => row.strategy_id === selectedStrategyId) || null,
    [strategies, selectedStrategyId],
  );

  const selectedGate = useMemo(
    () => strategyGateMap.get(selectedStrategyId) || null,
    [strategyGateMap, selectedStrategyId],
  );

  const liveReady = useMemo(() => {
    if (!liveStatus) return false;
    return Boolean(
      liveStatus.live_trading_enabled
      && liveStatus.has_private_key
      && liveStatus.has_funder
      && liveStatus.has_api_creds,
    );
  }, [liveStatus]);

  const canLimitOrder = useMemo(() => {
    return liveReady
      && confirmLive
      && !busyOrder
      && tokenId.trim().length > 0
      && price > 0
      && size > 0;
  }, [liveReady, confirmLive, busyOrder, tokenId, price, size]);

  const canMarketOrder = useMemo(() => {
    return liveReady
      && confirmLive
      && !busyOrder
      && tokenId.trim().length > 0
      && amount > 0;
  }, [liveReady, confirmLive, busyOrder, tokenId, amount]);

  const filteredSortedMarkets = useMemo(() => {
    const q = marketSearch.trim().toLowerCase();
    const rows = marketRows.filter((row) => {
      if (!q) return true;
      const name = String(row.name || '').toLowerCase();
      const en = String(row.name_en || '').toLowerCase();
      const marketId = String(row.market_id || '').toLowerCase();
      return name.includes(q) || en.includes(q) || marketId.includes(q);
    });
    rows.sort((a, b) => {
      if (marketSort === 'spread') return asNumber(b.spread_pct, 0) - asNumber(a.spread_pct, 0);
      if (marketSort === 'depth') return asNumber(b.depth_usdc, 0) - asNumber(a.depth_usdc, 0);
      return asNumber(b.volume_24h, 0) - asNumber(a.volume_24h, 0);
    });
    return rows;
  }, [marketRows, marketSearch, marketSort]);

  const marketTotalPages = useMemo(() => {
    return Math.max(1, Math.ceil(filteredSortedMarkets.length / 20));
  }, [filteredSortedMarkets.length]);

  const marketPageRows = useMemo(() => {
    const start = (marketPage - 1) * 20;
    return filteredSortedMarkets.slice(start, start + 20);
  }, [filteredSortedMarkets, marketPage]);

  const marketPageButtons = useMemo(() => {
    const pages = new Set<number>();
    pages.add(1);
    pages.add(marketTotalPages);
    for (let i = marketPage - 2; i <= marketPage + 2; i += 1) {
      if (i >= 1 && i <= marketTotalPages) {
        pages.add(i);
      }
    }
    return Array.from(pages).sort((a, b) => a - b);
  }, [marketPage, marketTotalPages]);

  const switchReady = Boolean(liveStatus?.live_trading_enabled);
  const credsMissing: string[] = [];
  if (!liveStatus?.has_private_key) credsMissing.push('私钥 ✗');
  if (!liveStatus?.has_funder) credsMissing.push('Funder ✗');
  if (!liveStatus?.has_api_creds) credsMissing.push('API三元组 ✗');
  const credsReady = credsMissing.length === 0 && !!liveStatus;

  function isHealthOk(x: LiveHealthResult | null): boolean {
    return !!x && x.ok !== false && !x.disabled;
  }

  const failedApis: string[] = [];
  if (balanceResult && !isHealthOk(balanceResult)) failedApis.push('余额');
  if (ordersResult && !isHealthOk(ordersResult)) failedApis.push('挂单');
  if (tradesResult && !isHealthOk(tradesResult)) failedApis.push('成交');
  const apiReady = !!balanceResult && !!ordersResult && !!tradesResult && failedApis.length === 0;
  const launchReady = switchReady && credsReady && apiReady;

  const loadStrategyPool = useCallback(async () => {
    const [sRes, gRes] = await Promise.all([
      apiGet<{ rows: StrategyRow[] }>('/strategies'),
      apiGet<LiveGateResponse | (LiveGateResponse & { count?: number })>('/quant/live-gate'),
    ]);
    const sRows = Array.isArray(sRes.rows) ? sRes.rows : [];
    const gRows = Array.isArray(gRes.rows) ? gRes.rows : [];
    setStrategies(sRows);
    setGateRows(gRows);
    setGatePassCount(asNumber(gRes.eligible_count, 0));
    setGateTotalCount(asNumber((gRes as { total_count?: number; count?: number }).total_count ?? (gRes as { count?: number }).count, gRows.length));
    if ((gRes as LiveGateResponse).thresholds) {
      const t = (gRes as LiveGateResponse).thresholds;
      setGateThresholds({
        min_hours: asNumber(t.min_hours, DEFAULT_GATE_THRESHOLDS.min_hours),
        min_win_rate: asNumber(t.min_win_rate, DEFAULT_GATE_THRESHOLDS.min_win_rate),
        min_pnl: asNumber(t.min_pnl, DEFAULT_GATE_THRESHOLDS.min_pnl),
        min_fills: asNumber(t.min_fills, DEFAULT_GATE_THRESHOLDS.min_fills),
      });
    }
    if (sRows.length === 0) {
      setSelectedStrategyId('');
      return;
    }
    if (!selectedStrategyId || !sRows.some((row) => row.strategy_id === selectedStrategyId)) {
      setSelectedStrategyId(sRows[0].strategy_id);
    }
  }, [selectedStrategyId]);

  const loadLiveStatus = useCallback(async () => {
    try {
      const out = await apiGet<LiveStatus>('/status');
      setLiveStatus(out);
    } catch {
      // Keep current status when endpoint is temporarily unavailable.
    }
  }, []);

  const runSelfCheck = useCallback(async () => {
    setChecking(true);
    try {
      const [bal, ord, trd] = await Promise.all([
        apiGet<LiveHealthResult>('/account/balance'),
        apiGet<LiveHealthResult>('/account/open-orders'),
        apiGet<LiveHealthResult>('/account/trades'),
      ]);
      setBalanceResult(bal);
      setOrdersResult(ord);
      setTradesResult(trd);
    } catch (err) {
      setBalanceResult({ ok: false, error: String((err as Error).message || err) });
      setOrdersResult((prev) => prev || { ok: false, error: '检测失败' });
      setTradesResult((prev) => prev || { ok: false, error: '检测失败' });
    } finally {
      setSelfCheckAt(new Date().toLocaleString('zh-CN', { hour12: false }));
      setChecking(false);
    }
  }, []);

  const loadCredentials = useCallback(async () => {
    setCredLoading(true);
    setCredError('');
    try {
      const data = await apiGet<LiveCredentialStatus>('/settings/live-credentials');
      setCredStatus(data);
      setCredForm((f) => ({
        ...f,
        live_trading_enabled: Boolean(data.live_trading_enabled),
        live_force_ack: Boolean(data.live_force_ack),
        live_max_order_usdc: Number(data.live_max_order_usdc) || 25,
        live_host: String(data.live_host || 'https://clob.polymarket.com'),
        chain_id: Number(data.chain_id) || 137,
        signature_type: Number(data.signature_type) || 0,
        private_key: '',
        funder: '',
        api_key: '',
        api_secret: '',
        api_passphrase: '',
      }));
    } catch (err) {
      setCredError(String((err as Error).message || err));
    } finally {
      setCredLoading(false);
    }
  }, []);

  const saveCredentials = useCallback(async () => {
    setCredSaving(true);
    setCredError('');
    setCredSaved(false);
    setCredRestartHint(null);
    try {
      const payload: Record<string, unknown> = {};
      const base = credStatus;

      if (!base || credForm.live_trading_enabled !== Boolean(base.live_trading_enabled)) {
        payload.live_trading_enabled = credForm.live_trading_enabled;
      }
      if (!base || credForm.live_force_ack !== Boolean(base.live_force_ack)) {
        payload.live_force_ack = credForm.live_force_ack;
      }
      if (!base || Number(credForm.live_max_order_usdc) !== Number(base.live_max_order_usdc)) {
        payload.live_max_order_usdc = Number(credForm.live_max_order_usdc);
      }
      if (!base || credForm.live_host.trim() !== String(base.live_host || '').trim()) {
        payload.live_host = credForm.live_host.trim();
      }
      if (!base || Number(credForm.chain_id) !== Number(base.chain_id)) {
        payload.chain_id = Number(credForm.chain_id);
      }
      if (!base || Number(credForm.signature_type) !== Number(base.signature_type)) {
        payload.signature_type = Number(credForm.signature_type);
      }

      if (credForm.private_key.trim()) payload.private_key = credForm.private_key.trim();
      if (credForm.funder.trim()) payload.funder = credForm.funder.trim();
      if (credForm.api_key.trim()) payload.api_key = credForm.api_key.trim();
      if (credForm.api_secret.trim()) payload.api_secret = credForm.api_secret.trim();
      if (credForm.api_passphrase.trim()) payload.api_passphrase = credForm.api_passphrase.trim();

      await apiPost('/settings/live-credentials', payload);
      const hint = await apiPost<{ message: string; command: string; note: string }>('/settings/restart-hint');
      setCredRestartHint(hint);
      setCredSaved(true);
      window.setTimeout(() => setCredSaved(false), 2200);

      setCredForm((f) => ({
        ...f,
        private_key: '',
        funder: '',
        api_key: '',
        api_secret: '',
        api_passphrase: '',
      }));
      await loadCredentials();
    } catch (err) {
      setCredError(String((err as Error).message || err));
    } finally {
      setCredSaving(false);
    }
  }, [credForm, credStatus, loadCredentials]);

  const loadBotStatus = useCallback(async () => {
    const out = await apiGet<BotStatus>('/bot/status');
    setBotStatus(out);
  }, []);

  const loadOrdersTrades = useCallback(async () => {
    const [oRes, tRes] = await Promise.all([
      apiGet<unknown>('/account/open-orders'),
      apiGet<unknown>('/account/trades'),
    ]);

    const ordersDisabled = oRes as ApiDisabled;
    const tradesDisabled = tRes as ApiDisabled;
    if (ordersDisabled && ordersDisabled.ok === false && ordersDisabled.disabled) {
      setOpenOrdersDisabled(ordersDisabled);
      setOpenOrders([]);
    } else {
      setOpenOrdersDisabled(null);
      setOpenOrders(extractRows<LiveOrderRow>(oRes));
    }

    if (tradesDisabled && tradesDisabled.ok === false && tradesDisabled.disabled) {
      setRecentTradesDisabled(tradesDisabled);
      setRecentTrades([]);
    } else {
      setRecentTradesDisabled(null);
      setRecentTrades(extractRows<LiveTradeRow>(tRes));
    }
  }, []);

  const loadStrategyDetail = useCallback(async (id: string, withLoading = false) => {
    const sid = String(id || '').trim();
    if (!sid) {
      setStrategyDetail(null);
      setStrategyTrades([]);
      setDetailError('');
      return;
    }
    if (withLoading) setDetailLoading(true);
    try {
      const [overview, trades] = await Promise.all([
        apiGet<StrategyOverviewResponse>(`/strategy/${encodeURIComponent(sid)}/overview`),
        apiGet<{ rows?: StrategyTradeRow[] }>(`/strategy/${encodeURIComponent(sid)}/trades?limit=10`),
      ]);
      setStrategyDetail(overview);
      setStrategyTrades(Array.isArray(trades.rows) ? trades.rows : []);
      setDetailError('');
    } catch (err) {
      setStrategyDetail(null);
      setStrategyTrades([]);
      setDetailError(String((err as Error).message || err));
    } finally {
      if (withLoading) setDetailLoading(false);
    }
  }, []);

  function selectStrategy(id: string) {
    setSelectedStrategyId(String(id || '').trim());
  }

  const loadMarketMonitor = useCallback(async () => {
    setMarketLoading(true);
    try {
      const out = await apiGet<{ rows?: MarketBrowserRow[] }>('/markets/monitor');
      setMarketRows(Array.isArray(out.rows) ? out.rows : []);
      setMarketError('');
      setRefreshAt(new Date().toLocaleString('zh-CN', { hour12: false }));
    } catch (err) {
      setMarketError(String((err as Error).message || err));
    } finally {
      setMarketLoading(false);
    }
  }, []);

  const refreshAll = useCallback(async () => {
    await Promise.all([
      loadStrategyPool(),
      loadLiveStatus(),
      loadBotStatus(),
      loadOrdersTrades(),
    ]);
    setRefreshAt(new Date().toLocaleString('zh-CN', { hour12: false }));
  }, [loadStrategyPool, loadLiveStatus, loadBotStatus, loadOrdersTrades]);

  useEffect(() => {
    void refreshAll();
  }, [refreshAll]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      void loadStrategyPool();
      void loadLiveStatus();
    }, 10000);
    return () => window.clearInterval(timer);
  }, [loadStrategyPool, loadLiveStatus]);

  useEffect(() => {
    if (activeTab !== 'tab2') return;
    if (filteredStrategies.length === 0) {
      setSelectedStrategyId('');
      setStrategyDetail(null);
      setStrategyTrades([]);
      return;
    }
    if (!selectedStrategyId || !filteredStrategies.some((row) => row.strategy_id === selectedStrategyId)) {
      setSelectedStrategyId(filteredStrategies[0].strategy_id);
    }
  }, [activeTab, filteredStrategies, selectedStrategyId]);

  useEffect(() => {
    if (activeTab !== 'tab2') return;
    if (!selectedStrategyId) {
      setStrategyDetail(null);
      setStrategyTrades([]);
      return;
    }
    void loadStrategyDetail(selectedStrategyId, true);
  }, [activeTab, selectedStrategyId, loadStrategyDetail]);

  useEffect(() => {
    if (activeTab !== 'tab2' || !selectedStrategyId) return;
    const timer = window.setInterval(() => {
      void loadStrategyDetail(selectedStrategyId, false);
    }, 15000);
    return () => window.clearInterval(timer);
  }, [activeTab, selectedStrategyId, loadStrategyDetail]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      void loadBotStatus();
    }, 3000);
    return () => window.clearInterval(timer);
  }, [loadBotStatus]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      void loadOrdersTrades();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [loadOrdersTrades]);

  useEffect(() => {
    void loadMarketMonitor();
    const timer = window.setInterval(() => {
      void loadMarketMonitor();
    }, 30000);
    return () => window.clearInterval(timer);
  }, [loadMarketMonitor]);

  useEffect(() => {
    void loadLiveStatus();
    void runSelfCheck();
    const timer = window.setInterval(() => {
      void loadLiveStatus();
      void runSelfCheck();
    }, 15000);
    return () => window.clearInterval(timer);
  }, [loadLiveStatus, runSelfCheck]);

  useEffect(() => {
    if (activeTab === 'tab1') {
      void loadCredentials();
    }
  }, [activeTab, loadCredentials]);

  useEffect(() => {
    setMarketPage(1);
  }, [marketSearch, marketSort]);

  useEffect(() => {
    if (marketPage > marketTotalPages) {
      setMarketPage(marketTotalPages);
    }
  }, [marketPage, marketTotalPages]);

  async function placeLimitOrder() {
    if (!canLimitOrder) return;
    setBusyOrder(true);
    try {
      const payload = {
        strategy_id: selectedStrategyId || '',
        token_id: tokenId.trim(),
        side,
        price: Number(price),
        size: Number(size),
        order_type: 'GTC',
        confirm_live: true,
      };
      const out = await apiPost<unknown>('/orders/limit', payload);
      setResultTone('ok');
      setResultText(JSON.stringify(out, null, 2));
      await loadOrdersTrades();
      window.setTimeout(() => setResultText(''), 2000);
    } catch (err) {
      setResultTone('bad');
      setResultText(String((err as Error).message || err));
    } finally {
      setBusyOrder(false);
    }
  }

  async function placeMarketOrder() {
    if (!canMarketOrder) return;
    setBusyOrder(true);
    try {
      const payload = {
        strategy_id: selectedStrategyId || '',
        token_id: tokenId.trim(),
        side,
        amount: Number(amount),
        order_type: 'FOK',
        confirm_live: true,
      };
      const out = await apiPost<unknown>('/orders/market', payload);
      setResultTone('ok');
      setResultText(JSON.stringify(out, null, 2));
      await loadOrdersTrades();
      window.setTimeout(() => setResultText(''), 2000);
    } catch (err) {
      setResultTone('bad');
      setResultText(String((err as Error).message || err));
    } finally {
      setBusyOrder(false);
    }
  }

  async function startBot() {
    if (!liveReady || !confirmLive || !botTokenId.trim() || busyBot) return;
    setBusyBot(true);
    try {
      const out = await apiPost<unknown>('/bot/start?confirm_live=true', {
        token_id: botTokenId.trim(),
        interval_sec: Number(botInterval),
      });
      setResultTone('ok');
      setResultText(JSON.stringify(out, null, 2));
      await loadBotStatus();
    } catch (err) {
      setResultTone('bad');
      setResultText(String((err as Error).message || err));
    } finally {
      setBusyBot(false);
    }
  }

  async function stopBot() {
    if (busyBot) return;
    setBusyBot(true);
    try {
      const out = await apiPost<unknown>('/bot/stop?confirm_live=true');
      setResultTone('ok');
      setResultText(JSON.stringify(out, null, 2));
      await loadBotStatus();
    } catch (err) {
      setResultTone('bad');
      setResultText(String((err as Error).message || err));
    } finally {
      setBusyBot(false);
    }
  }

  async function cancelAllOrders() {
    if (!confirmLive) return;
    if (!window.confirm('确认执行全撤单？该操作会取消当前所有实盘挂单。')) return;
    try {
      const out = await apiPost<unknown>('/orders/cancel-all?confirm_live=true');
      setResultTone('ok');
      setResultText(JSON.stringify(out, null, 2));
      await loadOrdersTrades();
    } catch (err) {
      setResultTone('bad');
      setResultText(String((err as Error).message || err));
    }
  }

  async function onSelectMarket(marketId: string) {
    if (!marketId) return;
    if (expandedMarketId === marketId) {
      setExpandedMarketId('');
      return;
    }
    setExpandedMarketId(marketId);
    if (tokenOptionsByMarket[marketId]) {
      return;
    }
    setMarketTokenLoadingId(marketId);
    setMarketTokenError((prev) => ({ ...prev, [marketId]: '' }));
    try {
      const out = await apiGet<{ rows?: PaperMarketRow[] }>('/paper/markets?limit=50');
      const rows = Array.isArray(out.rows) ? out.rows : [];
      const target = rows.find((row) => String(row.id || '') === marketId);
      if (!target) {
        setTokenOptionsByMarket((prev) => ({ ...prev, [marketId]: [] }));
        setMarketTokenError((prev) => ({
          ...prev,
          [marketId]: '未在 /api/paper/markets?limit=50 找到该市场，请手动输入 token_id。',
        }));
        return;
      }

      const options: TokenOption[] = [];
      const outcomes = Array.isArray(target.outcomes) ? target.outcomes : [];
      for (let i = 0; i < outcomes.length; i += 1) {
        const row = outcomes[i] || {};
        const token = String(row.token_id || '').trim();
        if (!token) continue;
        options.push({
          token_id: token,
          label: String(row.outcome || '').trim() || `Outcome ${i + 1}`,
          price: row.price,
        });
      }
      if (options.length === 0) {
        const ids = Array.isArray(target.token_ids) ? target.token_ids : [];
        for (let i = 0; i < ids.length; i += 1) {
          const token = String(ids[i] || '').trim();
          if (!token) continue;
          options.push({ token_id: token, label: `Token ${i + 1}` });
        }
      }
      const uniq = new Map<string, TokenOption>();
      for (const item of options) {
        if (!uniq.has(item.token_id)) {
          uniq.set(item.token_id, item);
        }
      }
      const finalRows = Array.from(uniq.values());
      setTokenOptionsByMarket((prev) => ({ ...prev, [marketId]: finalRows }));
      if (finalRows.length === 0) {
        setMarketTokenError((prev) => ({
          ...prev,
          [marketId]: '该市场未返回 token_id，请手动输入 token_id。',
        }));
      }
    } catch (err) {
      setTokenOptionsByMarket((prev) => ({ ...prev, [marketId]: [] }));
      setMarketTokenError((prev) => ({
        ...prev,
        [marketId]: `拉取 token 失败: ${String((err as Error).message || err)}`,
      }));
    } finally {
      setMarketTokenLoadingId('');
    }
  }

  function useTokenFromMarket(token: string) {
    if (!token.trim()) return;
    setTokenId(token.trim());
    setActiveTab('tab2');
  }

  const formInputClass = 'w-full bg-[#0d1117] border border-dashboard-line rounded-lg px-3 py-2 text-sm text-dashboard-text placeholder:text-dashboard-muted/50 focus:border-sky-500 focus:outline-none';

  function credentialState(hasConfigured: boolean, currentValue: string): { text: string; cls: string } {
    if (currentValue.trim()) return { text: '✏️ 待保存', cls: 'text-sky-300' };
    if (hasConfigured) return { text: '✅ 已配置', cls: 'text-dashboard-good' };
    return { text: '❌ 未配置', cls: 'text-dashboard-bad' };
  }

  return (
    <div className="min-h-screen bg-dashboard-bg text-dashboard-text px-5 py-4 space-y-4">
      <header className="card px-4 py-3 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold m-0">实盘交易中心</h1>
          <div className="text-xs text-dashboard-muted mt-1">策略准入、实盘执行、Bot 控制与账户订单监控</div>
        </div>
        <div className="flex items-center gap-2">
          <a href="/dashboard" className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563]">仪表盘</a>
          <button
            onClick={() => void refreshAll()}
            className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563] inline-flex items-center gap-1"
          >
            <RefreshCw size={14} />
            刷新
          </button>
        </div>
      </header>

      <section className="card p-3">
        <div className="flex items-center gap-2">
          <button
            onClick={() => setActiveTab('tab1')}
            className={`rounded-lg border px-3 py-1.5 text-sm ${activeTab === 'tab1' ? 'border-sky-400 bg-sky-500/15 text-sky-300' : 'border-dashboard-line bg-[#111827] text-dashboard-text'}`}
          >
            Tab 1：总览
          </button>
          <button
            onClick={() => setActiveTab('tab2')}
            className={`rounded-lg border px-3 py-1.5 text-sm ${activeTab === 'tab2' ? 'border-orange-400 bg-orange-500/15 text-orange-300' : 'border-dashboard-line bg-[#111827] text-dashboard-text'}`}
          >
            Tab 2：实盘执行
          </button>
          <button
            onClick={() => setActiveTab('tab3')}
            className={`rounded-lg border px-3 py-1.5 text-sm ${activeTab === 'tab3' ? 'border-sky-400 bg-sky-500/15 text-sky-300' : 'border-dashboard-line bg-[#111827] text-dashboard-text'}`}
          >
            Tab 3：市场浏览
          </button>
          <div className="ml-auto text-xs text-dashboard-muted">最近刷新: {refreshAt || '-'}</div>
        </div>
      </section>

      {activeTab === 'tab1' ? (
        <section className="space-y-3">
          <div className="grid grid-cols-4 gap-3">
            <article className="card p-3">
              <div className="text-xs text-dashboard-muted">实盘开关</div>
              <div className={`mt-2 text-xl font-semibold ${switchReady ? 'text-dashboard-good' : 'text-dashboard-bad'}`}>
                {switchReady ? '已开启' : '未开启'}
              </div>
              <div className="text-xs text-dashboard-muted mt-2">live_trading_enabled={String(Boolean(liveStatus?.live_trading_enabled))}</div>
            </article>

            <article className="card p-3">
              <div className="text-xs text-dashboard-muted">凭证完整性</div>
              <div className={`mt-2 text-xl font-semibold ${credsReady ? 'text-dashboard-good' : 'text-dashboard-bad'}`}>
                {credsReady ? '完整' : '缺失'}
              </div>
              <div className="text-xs text-dashboard-muted mt-2 space-y-1">
                {credsReady ? (
                  <div className="text-dashboard-good">私钥 / Funder / API 三元组均已配置</div>
                ) : (
                  credsMissing.map((row) => <div key={row}>{row}</div>)
                )}
              </div>
            </article>

            <article className="card p-3">
              <div className="text-xs text-dashboard-muted">交易接口连通</div>
              <div className={`mt-2 text-xl font-semibold ${apiReady ? 'text-dashboard-good' : 'text-dashboard-bad'}`}>
                {apiReady ? '正常' : '异常'}
              </div>
              <div className="text-xs text-dashboard-muted mt-2 space-y-1">
                {!balanceResult && !ordersResult && !tradesResult ? <div>尚未检测</div> : null}
                {failedApis.length > 0 ? <div className="text-dashboard-bad">失败接口: {failedApis.join(' / ')}</div> : null}
                {failedApis.length === 0 && (balanceResult || ordersResult || tradesResult) ? (
                  <div className="text-dashboard-good">余额 / 委托 / 成交接口均可用</div>
                ) : null}
              </div>
            </article>

            <article className="card p-3">
              <div className="text-xs text-dashboard-muted">实盘启动条件</div>
              <div className={`mt-2 text-xl font-semibold ${launchReady ? 'text-dashboard-good' : 'text-dashboard-bad'}`}>
                {launchReady ? '已就绪' : '未就绪'}
              </div>
              <div className="text-xs text-dashboard-muted mt-2 space-y-1">
                <div>检测时间: {selfCheckAt || '-'}</div>
                {launchReady ? null : (
                  <div className="text-dashboard-bad">
                    {failedApis.length > 0 ? `失败接口: ${failedApis.join(' / ')}` : '请先完成开关、凭证与接口检测'}
                  </div>
                )}
              </div>
            </article>
          </div>

          <article className="card p-3">
            <button
              onClick={() => setGuideOpen((v) => !v)}
              className="w-full flex items-center justify-between text-left"
            >
              <div>
                <div className="text-sm font-semibold">配置指引（折叠式）</div>
                <div className="text-xs text-dashboard-muted mt-1">CLI 初始化 → .env 配置 → 启动与验证</div>
              </div>
              <span className="text-xs text-dashboard-muted">{guideOpen ? '收起 ▲' : '展开 ▼'}</span>
            </button>
            {guideOpen ? (
              <div className="mt-3 space-y-3">
                <div>
                  <div className="text-xs text-dashboard-muted mb-1">Step 1：安装并创建 CLOB API</div>
                  <pre className="bg-[#0d1117] rounded-lg p-4 font-mono text-xs text-gray-300 overflow-x-auto whitespace-pre leading-relaxed">{`brew tap Polymarket/polymarket-cli
brew install polymarket
polymarket setup
polymarket wallet import <你的私钥>
polymarket approve check
polymarket approve set
polymarket clob create-api-key
polymarket clob account-status`}</pre>
                </div>
                <div>
                  <div className="text-xs text-dashboard-muted mb-1">Step 2：填写 .env</div>
                  <pre className="bg-[#0d1117] rounded-lg p-4 font-mono text-xs text-gray-300 overflow-x-auto whitespace-pre leading-relaxed">{`LIVE_TRADING_ENABLED=true
LIVE_FORCE_ACK=true
LIVE_MAX_ORDER_USDC=5

POLYMARKET_LIVE_HOST=https://clob.polymarket.com
POLYMARKET_CHAIN_ID=137
POLYMARKET_SIGNATURE_TYPE=0
POLYMARKET_PRIVATE_KEY=<你的私钥>
POLYMARKET_FUNDER=<你的钱包地址>
POLYMARKET_API_KEY=<API Key>
POLYMARKET_API_SECRET=<API Secret>
POLYMARKET_API_PASSPHRASE=<API Passphrase>`}</pre>
                </div>
                <div>
                  <div className="text-xs text-dashboard-muted mb-1">Step 3：启动并验证</div>
                  <pre className="bg-[#0d1117] rounded-lg p-4 font-mono text-xs text-gray-300 overflow-x-auto whitespace-pre leading-relaxed">{`cd /Users/chenweibin/Documents/saima\\ polymarkt
./run_live_site.sh
curl http://127.0.0.1:8780/api/account/balance`}</pre>
                </div>
              </div>
            ) : null}
          </article>

          <article className="card p-3 space-y-4">
            <div className="flex items-center justify-between">
              <div>
                <div className="text-sm font-semibold">凭证配置</div>
                <div className="text-xs text-dashboard-muted mt-1">在此直接输入 Polymarket 实盘凭证，保存后重启服务生效。</div>
              </div>
              <div className="text-xs text-dashboard-muted">{credLoading ? '[加载中]' : ''}</div>
            </div>

            <div className="card p-3">
              <div className="text-sm font-medium text-dashboard-muted mb-3 flex items-center gap-2">
                <Shield size={14} />
                基础设置
              </div>
              <div className="grid grid-cols-3 gap-3 items-center mb-3">
                <div className="text-sm text-dashboard-muted">实盘开关</div>
                <div className="col-span-2 flex gap-2">
                  <button
                    onClick={() => setCredForm((f) => ({ ...f, live_trading_enabled: true }))}
                    className={`px-3 py-1 rounded text-sm ${credForm.live_trading_enabled ? 'bg-dashboard-good/20 text-dashboard-good border border-dashboard-good/50' : 'bg-[#111827] border border-dashboard-line text-dashboard-muted'}`}
                  >
                    开启
                  </button>
                  <button
                    onClick={() => setCredForm((f) => ({ ...f, live_trading_enabled: false }))}
                    className={`px-3 py-1 rounded text-sm ${!credForm.live_trading_enabled ? 'bg-dashboard-bad/20 text-dashboard-bad border border-dashboard-bad/50' : 'bg-[#111827] border border-dashboard-line text-dashboard-muted'}`}
                  >
                    关闭
                  </button>
                </div>
              </div>
              <div className="grid grid-cols-3 gap-3 items-center mb-3">
                <div className="text-sm text-dashboard-muted">二次确认</div>
                <div className="col-span-2 flex gap-2">
                  <button
                    onClick={() => setCredForm((f) => ({ ...f, live_force_ack: true }))}
                    className={`px-3 py-1 rounded text-sm ${credForm.live_force_ack ? 'bg-dashboard-good/20 text-dashboard-good border border-dashboard-good/50' : 'bg-[#111827] border border-dashboard-line text-dashboard-muted'}`}
                  >
                    开启
                  </button>
                  <button
                    onClick={() => setCredForm((f) => ({ ...f, live_force_ack: false }))}
                    className={`px-3 py-1 rounded text-sm ${!credForm.live_force_ack ? 'bg-dashboard-bad/20 text-dashboard-bad border border-dashboard-bad/50' : 'bg-[#111827] border border-dashboard-line text-dashboard-muted'}`}
                  >
                    关闭
                  </button>
                </div>
              </div>
              <div className="grid grid-cols-3 gap-3 items-center">
                <div className="text-sm text-dashboard-muted">单笔上限(USDC)</div>
                <div className="col-span-2">
                  <input
                    type="number"
                    step="0.0001"
                    value={credForm.live_max_order_usdc}
                    onChange={(e) => setCredForm((f) => ({ ...f, live_max_order_usdc: asNumber(e.target.value, 25) }))}
                    className={formInputClass}
                  />
                </div>
              </div>
            </div>

            <div className="card p-3">
              <div className="text-sm font-medium text-dashboard-muted mb-3 flex items-center gap-2">
                <Key size={14} />
                网络配置
              </div>
              <div className="grid grid-cols-3 gap-3 items-center mb-3">
                <div className="text-sm text-dashboard-muted">Host</div>
                <div className="col-span-2">
                  <input
                    value={credForm.live_host}
                    onChange={(e) => setCredForm((f) => ({ ...f, live_host: e.target.value }))}
                    className={formInputClass}
                  />
                </div>
              </div>
              <div className="grid grid-cols-3 gap-3 items-center mb-3">
                <div className="text-sm text-dashboard-muted">Chain ID</div>
                <div className="col-span-2">
                  <input
                    type="number"
                    value={credForm.chain_id}
                    onChange={(e) => setCredForm((f) => ({ ...f, chain_id: asNumber(e.target.value, 137) }))}
                    className={formInputClass}
                  />
                </div>
              </div>
              <div className="grid grid-cols-3 gap-3 items-center">
                <div className="text-sm text-dashboard-muted">Signature Type</div>
                <div className="col-span-2">
                  <input
                    type="number"
                    value={credForm.signature_type}
                    onChange={(e) => setCredForm((f) => ({ ...f, signature_type: asNumber(e.target.value, 0) }))}
                    className={formInputClass}
                  />
                </div>
              </div>
            </div>

            <div className="card p-3">
              <div className="text-sm font-medium text-dashboard-muted mb-3 flex items-center gap-2">
                <Shield size={14} />
                钱包凭证
                <span className="text-[11px] text-dashboard-muted">🔒 私钥仅保存在本地 .env 文件中，不会上传到任何服务器。</span>
              </div>
              <div className="grid grid-cols-12 gap-3 items-center mb-3">
                <div className="col-span-2 text-sm text-dashboard-muted">Private Key</div>
                <div className="col-span-8 relative">
                  <input
                    type={showPK ? 'text' : 'password'}
                    value={credForm.private_key}
                    onChange={(e) => setCredForm((f) => ({ ...f, private_key: e.target.value }))}
                    placeholder={credStatus?.has_private_key ? `${credStatus.private_key_masked || '***'}（已配置）` : '未配置，请输入'}
                    className={`${formInputClass} pr-10`}
                  />
                  <button
                    onClick={() => setShowPK((v) => !v)}
                    className="absolute right-2 top-1/2 -translate-y-1/2 text-dashboard-muted hover:text-dashboard-text"
                  >
                    {showPK ? <EyeOff size={15} /> : <Eye size={15} />}
                  </button>
                </div>
                <div className={`col-span-2 text-xs ${credentialState(Boolean(credStatus?.has_private_key), credForm.private_key).cls}`}>
                  {credentialState(Boolean(credStatus?.has_private_key), credForm.private_key).text}
                </div>
              </div>
              <div className="grid grid-cols-12 gap-3 items-center">
                <div className="col-span-2 text-sm text-dashboard-muted">Funder 地址</div>
                <div className="col-span-8">
                  <input
                    value={credForm.funder}
                    onChange={(e) => setCredForm((f) => ({ ...f, funder: e.target.value }))}
                    placeholder={credStatus?.has_funder ? `${credStatus.funder_masked || '***'}（已配置）` : '未配置，请输入'}
                    className={formInputClass}
                  />
                </div>
                <div className={`col-span-2 text-xs ${credentialState(Boolean(credStatus?.has_funder), credForm.funder).cls}`}>
                  {credentialState(Boolean(credStatus?.has_funder), credForm.funder).text}
                </div>
              </div>
            </div>

            <div className="card p-3">
              <div className="text-sm font-medium text-dashboard-muted mb-3 flex items-center gap-2">
                <Key size={14} />
                API 凭证（polymarket clob create-api-key 生成）
              </div>
              <div className="grid grid-cols-12 gap-3 items-center mb-3">
                <div className="col-span-2 text-sm text-dashboard-muted">API Key</div>
                <div className="col-span-8 relative">
                  <input
                    type={showApiKey ? 'text' : 'password'}
                    value={credForm.api_key}
                    onChange={(e) => setCredForm((f) => ({ ...f, api_key: e.target.value }))}
                    placeholder={credStatus?.has_api_key ? `${credStatus.api_key_masked || '***'}（已配置）` : '未配置，请输入'}
                    className={`${formInputClass} pr-10`}
                  />
                  <button
                    onClick={() => setShowApiKey((v) => !v)}
                    className="absolute right-2 top-1/2 -translate-y-1/2 text-dashboard-muted hover:text-dashboard-text"
                  >
                    {showApiKey ? <EyeOff size={15} /> : <Eye size={15} />}
                  </button>
                </div>
                <div className={`col-span-2 text-xs ${credentialState(Boolean(credStatus?.has_api_key), credForm.api_key).cls}`}>
                  {credentialState(Boolean(credStatus?.has_api_key), credForm.api_key).text}
                </div>
              </div>
              <div className="grid grid-cols-12 gap-3 items-center mb-3">
                <div className="col-span-2 text-sm text-dashboard-muted">API Secret</div>
                <div className="col-span-8 relative">
                  <input
                    type={showSecret ? 'text' : 'password'}
                    value={credForm.api_secret}
                    onChange={(e) => setCredForm((f) => ({ ...f, api_secret: e.target.value }))}
                    placeholder={credStatus?.has_api_secret ? `${credStatus.api_secret_masked || '***'}（已配置）` : '未配置，请输入'}
                    className={`${formInputClass} pr-10`}
                  />
                  <button
                    onClick={() => setShowSecret((v) => !v)}
                    className="absolute right-2 top-1/2 -translate-y-1/2 text-dashboard-muted hover:text-dashboard-text"
                  >
                    {showSecret ? <EyeOff size={15} /> : <Eye size={15} />}
                  </button>
                </div>
                <div className={`col-span-2 text-xs ${credentialState(Boolean(credStatus?.has_api_secret), credForm.api_secret).cls}`}>
                  {credentialState(Boolean(credStatus?.has_api_secret), credForm.api_secret).text}
                </div>
              </div>
              <div className="grid grid-cols-12 gap-3 items-center">
                <div className="col-span-2 text-sm text-dashboard-muted">API Passphrase</div>
                <div className="col-span-8 relative">
                  <input
                    type={showPass ? 'text' : 'password'}
                    value={credForm.api_passphrase}
                    onChange={(e) => setCredForm((f) => ({ ...f, api_passphrase: e.target.value }))}
                    placeholder={credStatus?.has_api_passphrase ? `${credStatus.api_passphrase_masked || '***'}（已配置）` : '未配置，请输入'}
                    className={`${formInputClass} pr-10`}
                  />
                  <button
                    onClick={() => setShowPass((v) => !v)}
                    className="absolute right-2 top-1/2 -translate-y-1/2 text-dashboard-muted hover:text-dashboard-text"
                  >
                    {showPass ? <EyeOff size={15} /> : <Eye size={15} />}
                  </button>
                </div>
                <div className={`col-span-2 text-xs ${credentialState(Boolean(credStatus?.has_api_passphrase), credForm.api_passphrase).cls}`}>
                  {credentialState(Boolean(credStatus?.has_api_passphrase), credForm.api_passphrase).text}
                </div>
              </div>
            </div>

            <div className="flex items-center gap-3">
              <button
                onClick={() => void saveCredentials()}
                disabled={credSaving}
                className={`rounded-lg px-6 py-2.5 text-white font-medium inline-flex items-center gap-2 disabled:opacity-40 disabled:cursor-not-allowed ${
                  credSaved ? 'bg-emerald-600 hover:bg-emerald-500' : 'bg-orange-600 hover:bg-orange-500'
                }`}
              >
                <Save size={15} />
                {credSaving ? '保存中...' : '💾 保存凭证'}
              </button>
              {credError ? <div className="text-sm text-dashboard-bad break-all">{credError}</div> : null}
            </div>

            {credSaved || credRestartHint ? (
              <div className="rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-3 py-3 text-sm text-emerald-200 space-y-2">
                <div>✅ 凭证已保存到 .env 文件</div>
                <div>⚠ 请手动重启服务使配置生效：</div>
                <pre className="bg-[#0d1117] rounded-lg p-3 font-mono text-xs text-gray-300 overflow-x-auto whitespace-pre leading-relaxed">{credRestartHint?.command || 'cd /Users/chenweibin/Documents/saima\\ polymarkt && ./run_live_site.sh'}</pre>
                <div>{credRestartHint?.note || '重启后点击上方"重新检测"验证配置。'}</div>
              </div>
            ) : null}
          </article>

          <article className="card p-3 space-y-3">
            <div className="flex items-center justify-between">
              <div>
                <div className="text-sm font-semibold">实时自检结果</div>
                <div className="text-xs text-dashboard-muted mt-1">最近检测: {selfCheckAt || '-'}</div>
              </div>
              <div className="flex items-center gap-3">
                <div className="text-[11px] text-dashboard-muted">
                  Host: {liveStatus?.host || '-'} | Chain: {liveStatus?.chain_id ?? '-'} | SigType: {liveStatus?.signature_type ?? '-'}
                </div>
                <button
                  onClick={() => {
                    void loadLiveStatus();
                    void runSelfCheck();
                  }}
                  disabled={checking}
                  className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563] inline-flex items-center gap-1 disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  <RefreshCw size={14} className={checking ? 'animate-spin' : ''} />
                  {checking ? '检测中...' : '🔄 重新检测'}
                </button>
              </div>
            </div>

            <div className="grid grid-cols-3 gap-3">
              <article className="card p-3">
                <div className="text-xs text-dashboard-muted">余额接口</div>
                <div className={`mt-2 text-lg font-semibold ${isHealthOk(balanceResult) ? 'text-dashboard-good' : 'text-dashboard-bad'}`}>
                  {isHealthOk(balanceResult) ? '✅ 成功' : '❌ 失败'}
                </div>
                {!isHealthOk(balanceResult) ? (
                  <div className="text-xs text-dashboard-muted mt-2 break-all">
                    {balanceResult?.reason || balanceResult?.error || '尚未检测'}
                  </div>
                ) : null}
              </article>

              <article className="card p-3">
                <div className="text-xs text-dashboard-muted">委托接口</div>
                <div className={`mt-2 text-lg font-semibold ${isHealthOk(ordersResult) ? 'text-dashboard-good' : 'text-dashboard-bad'}`}>
                  {isHealthOk(ordersResult) ? '✅ 成功' : '❌ 失败'}
                </div>
                {!isHealthOk(ordersResult) ? (
                  <div className="text-xs text-dashboard-muted mt-2 break-all">
                    {ordersResult?.reason || ordersResult?.error || '尚未检测'}
                  </div>
                ) : null}
              </article>

              <article className="card p-3">
                <div className="text-xs text-dashboard-muted">成交接口</div>
                <div className={`mt-2 text-lg font-semibold ${isHealthOk(tradesResult) ? 'text-dashboard-good' : 'text-dashboard-bad'}`}>
                  {isHealthOk(tradesResult) ? '✅ 成功' : '❌ 失败'}
                </div>
                {!isHealthOk(tradesResult) ? (
                  <div className="text-xs text-dashboard-muted mt-2 break-all">
                    {tradesResult?.reason || tradesResult?.error || '尚未检测'}
                  </div>
                ) : null}
              </article>
            </div>

            <div className="rounded-lg border border-yellow-500/40 bg-yellow-500/10 px-3 py-2 text-sm text-yellow-200">
              ⚠ 风险提示：先小额测试（1-5 USDC），确认成交与撤单正常后再放量。
            </div>
          </article>
        </section>
      ) : null}

      {activeTab === 'tab2' ? (
        <section className="space-y-4">
          <article className="card p-4">
            <div className="text-sm font-medium text-dashboard-muted mb-3">
              📋 策略筛选（从模拟盘选择已验证的策略进行实盘交易）
            </div>
            <div className="grid grid-cols-3 gap-4" style={{ minHeight: '500px' }}>
              <div className="col-span-1 border-r border-dashboard-line pr-4">
                <div className="rounded-lg border border-dashboard-line bg-[#111827] p-2 mb-2">
                  <label className="flex items-center gap-2 text-xs text-dashboard-muted px-1 py-1">
                    <input
                      type="checkbox"
                      checked={showProfitableOnly}
                      onChange={(e) => setShowProfitableOnly(e.target.checked)}
                      className="rounded"
                    />
                    仅显示盈利策略
                  </label>
                  <div className="text-xs text-dashboard-muted px-1">
                    通过门禁: <span className="text-dashboard-good">{gatePassCount}</span> / 总计: {gateTotalCount}
                  </div>
                </div>
                <div className="space-y-1 overflow-auto max-h-[500px] scroll-dark">
                  {filteredStrategies.map((row) => {
                    const gate = strategyGateMap.get(row.strategy_id);
                    const isSelected = selectedStrategyId === row.strategy_id;
                    const pnl = asNumber(row.total_pnl, 0);
                    const reasons = Array.isArray(gate?.reasons) ? gate?.reasons.join(' | ') : '';
                    const statusText = row.status === 'running' ? '🟢 运行中' : row.status === 'paused' ? '🟡 已暂停' : '🔴 已停止';
                    return (
                      <button
                        key={row.strategy_id}
                        onClick={() => selectStrategy(row.strategy_id)}
                        title={reasons}
                        className={`w-full text-left px-3 py-2 rounded-lg border transition-all ${
                          isSelected
                            ? 'border-sky-500 bg-sky-500/10'
                            : 'border-dashboard-line bg-[#111827] hover:border-[#4b5563]'
                        }`}
                      >
                        <div className="text-xs text-dashboard-muted font-mono truncate">{row.strategy_id}</div>
                        <div className="text-sm font-medium truncate">{row.name || row.strategy_id}</div>
                        <div className={`text-lg font-bold ${pnl >= 0 ? 'text-dashboard-good' : 'text-dashboard-bad'}`}>
                          {pnl >= 0 ? '+' : ''}
                          {pnl.toFixed(4)}
                        </div>
                        <div className="flex items-center gap-3 text-xs text-dashboard-muted">
                          <span>{statusText}</span>
                          <span>{asNumber(row.trade_count, 0)}笔</span>
                          <span>{(asNumber(row.win_rate, 0) * 100).toFixed(0)}%胜率</span>
                        </div>
                        {gate ? (
                          <div className={`mt-1 text-xs ${gate.eligible ? 'text-dashboard-good' : 'text-yellow-300'}`}>
                            {gate.eligible ? '✅ 已通过72h门禁' : '⚠ 未通过72h门禁'}
                          </div>
                        ) : null}
                      </button>
                    );
                  })}
                  {filteredStrategies.length === 0 ? (
                    <div className="text-dashboard-muted text-xs text-center py-6">暂无符合筛选条件的策略</div>
                  ) : null}
                </div>
              </div>

              <div className="col-span-2">
                {detailLoading ? (
                  <div className="h-full min-h-[500px] flex items-center justify-center text-dashboard-muted text-sm">
                    正在加载策略详情...
                  </div>
                ) : selectedStrategyId && strategyDetail ? (
                  <div className="space-y-4">
                    <div>
                      <div className="text-lg font-semibold">{strategyDetail.strategy?.name || selectedStrategyId}</div>
                      <div className="text-xs text-dashboard-muted mt-1">
                        {strategyDetail.strategy?.strategy_type || '-'} | 来源: {strategyDetail.strategy?.source || '-'} | 状态:{' '}
                        <span className={statusClass(strategyDetail.strategy?.status || '')}>{strategyDetail.strategy?.status || '-'}</span> | 运行:{' '}
                        {asNumber(strategyDetail.metrics?.runtime_hours, asNumber(selectedGate?.runtime_hours, 0)).toFixed(1)}h
                      </div>
                    </div>

                    <div className="grid grid-cols-3 gap-2">
                      <div className="card p-2">
                        <div className="text-xs text-dashboard-muted">总 PnL</div>
                        <div className={`text-sm font-bold ${asNumber(strategyDetail.metrics?.total_pnl, 0) >= 0 ? 'text-dashboard-good' : 'text-dashboard-bad'}`}>
                          {asNumber(strategyDetail.metrics?.total_pnl, 0).toFixed(4)}
                        </div>
                      </div>
                      <div className="card p-2">
                        <div className="text-xs text-dashboard-muted">胜率</div>
                        <div className="text-sm font-bold">{(asNumber(strategyDetail.metrics?.win_rate, 0) * 100).toFixed(1)}%</div>
                      </div>
                      <div className="card p-2">
                        <div className="text-xs text-dashboard-muted">交易数</div>
                        <div className="text-sm font-bold">{asNumber(strategyDetail.metrics?.trade_count, asNumber(selectedGate?.fills_count, 0))}</div>
                      </div>
                      <div className="card p-2">
                        <div className="text-xs text-dashboard-muted">今日 PnL</div>
                        <div className={`text-sm font-bold ${asNumber(strategyDetail.metrics?.today_pnl, 0) >= 0 ? 'text-dashboard-good' : 'text-dashboard-bad'}`}>
                          {asNumber(strategyDetail.metrics?.today_pnl, 0).toFixed(4)}
                        </div>
                      </div>
                      <div className="card p-2">
                        <div className="text-xs text-dashboard-muted">最大回撤</div>
                        <div className="text-sm font-bold">{asNumber(strategyDetail.metrics?.max_drawdown, 0).toFixed(3)}%</div>
                      </div>
                      <div className="card p-2">
                        <div className="text-xs text-dashboard-muted">盈利因子</div>
                        <div className="text-sm font-bold">{asNumber(strategyDetail.metrics?.profit_factor, 0).toFixed(2)}</div>
                      </div>
                    </div>

                    <div className="card p-3">
                      <div className="text-sm font-medium text-dashboard-muted mb-2">72h 门禁检查</div>
                      {(() => {
                        const runtimeVal = asNumber(strategyDetail.metrics?.runtime_hours, asNumber(selectedGate?.runtime_hours, 0));
                        const winRateVal = asNumber(strategyDetail.metrics?.win_rate, asNumber(selectedGate?.win_rate, 0));
                        const fillsVal = asNumber(strategyDetail.metrics?.trade_count, asNumber(selectedGate?.fills_count, 0));
                        const pnlVal = asNumber(strategyDetail.metrics?.total_pnl, asNumber(selectedGate?.pnl_total, 0));
                        const checks = [
                          {
                            label: `运行时长 ≥ ${asNumber(gateThresholds.min_hours, 72)}h`,
                            pass: runtimeVal >= asNumber(gateThresholds.min_hours, 72),
                            value: `${runtimeVal.toFixed(1)}h`,
                          },
                          {
                            label: `胜率 ≥ ${(asNumber(gateThresholds.min_win_rate, 0.45) * 100).toFixed(0)}%`,
                            pass: winRateVal >= asNumber(gateThresholds.min_win_rate, 0.45),
                            value: `${(winRateVal * 100).toFixed(1)}%`,
                          },
                          {
                            label: `成交 ≥ ${asNumber(gateThresholds.min_fills, 20)}笔`,
                            pass: fillsVal >= asNumber(gateThresholds.min_fills, 20),
                            value: `${fillsVal}笔`,
                          },
                          {
                            label: `PnL ≥ ${asNumber(gateThresholds.min_pnl, 0).toFixed(4)}`,
                            pass: pnlVal >= asNumber(gateThresholds.min_pnl, 0),
                            value: pnlVal.toFixed(4),
                          },
                        ];
                        const allPass = checks.every((c) => c.pass);
                        return (
                          <div className="space-y-1.5">
                            {checks.map((c, i) => (
                              <div key={`${selectedStrategyId}-check-${i}`} className="flex items-center gap-2 text-xs">
                                <span className={c.pass ? 'text-dashboard-good' : 'text-dashboard-bad'}>
                                  {c.pass ? '✅' : '❌'}
                                </span>
                                <span className="text-dashboard-muted">{c.label}:</span>
                                <span className={c.pass ? 'text-dashboard-good' : 'text-dashboard-bad'}>{c.value}</span>
                              </div>
                            ))}
                            {Array.isArray(selectedGate?.reasons) && selectedGate?.reasons.length > 0 ? (
                              <div className="text-xs text-dashboard-muted pt-1">系统门禁原因: {selectedGate.reasons.join(' | ')}</div>
                            ) : null}
                            <div className={`mt-2 text-xs font-medium ${allPass ? 'text-dashboard-good' : 'text-yellow-400'}`}>
                              {allPass ? '✅ 已通过门禁，可以进行实盘交易' : '⚠ 未通过门禁，建议继续在模拟盘验证'}
                            </div>
                          </div>
                        );
                      })()}
                    </div>

                    <div className="card p-3">
                      <div className="text-sm font-medium text-dashboard-muted mb-2">最近交易记录（最近10笔）</div>
                      <div className="overflow-auto max-h-[220px] scroll-dark">
                        <table className="w-full text-xs">
                          <thead className="text-dashboard-muted bg-[#111827]">
                            <tr>
                              <th className="text-left px-2 py-1.5 font-medium">时间</th>
                              <th className="text-left px-2 py-1.5 font-medium">方向</th>
                              <th className="text-left px-2 py-1.5 font-medium">市场</th>
                              <th className="text-left px-2 py-1.5 font-medium">价格</th>
                              <th className="text-left px-2 py-1.5 font-medium">数量</th>
                              <th className="text-left px-2 py-1.5 font-medium">盈亏</th>
                            </tr>
                          </thead>
                          <tbody>
                            {strategyTrades.map((t, i) => {
                              const sideText = String(t.side || '').toUpperCase();
                              return (
                                <tr key={`${selectedStrategyId}-trade-${i}`} className="border-t border-dashboard-line">
                                  <td className="px-2 py-1.5 whitespace-nowrap">{toLocalTime(String(t.time_utc || ''))}</td>
                                  <td className="px-2 py-1.5">
                                    <span className={sideText === 'BUY' ? 'text-dashboard-good' : 'text-dashboard-bad'}>{sideText || '-'}</span>
                                  </td>
                                  <td className="px-2 py-1.5 max-w-[220px] truncate" title={t.market_en || t.market}>
                                    {t.market || t.market_en || '-'}
                                  </td>
                                  <td className="px-2 py-1.5">{asNumber(t.price, 0).toFixed(4)}</td>
                                  <td className="px-2 py-1.5">{asNumber(t.cost_usdc, asNumber(t.quantity, 0)).toFixed(2)}</td>
                                  <td className={`px-2 py-1.5 ${asNumber(t.pnl, 0) >= 0 ? 'text-dashboard-good' : 'text-dashboard-bad'}`}>
                                    {asNumber(t.pnl, 0).toFixed(4)}
                                  </td>
                                </tr>
                              );
                            })}
                          </tbody>
                        </table>
                        {strategyTrades.length === 0 ? (
                          <div className="text-dashboard-muted text-xs text-center py-4">暂无交易记录</div>
                        ) : null}
                      </div>
                    </div>

                    <button
                      onClick={() => {
                        setSelectedStrategyId(selectedStrategyId);
                        document.getElementById('live-operation-area')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
                      }}
                      className="w-full rounded-lg bg-sky-600 hover:bg-sky-500 px-4 py-2.5 text-white font-medium inline-flex items-center justify-center gap-2"
                    >
                      🚀 选用此策略进行实盘交易
                    </button>
                  </div>
                ) : (
                  <div className="h-full min-h-[500px] flex items-center justify-center text-dashboard-muted text-sm">
                    {detailError ? `策略详情加载失败: ${detailError}` : '← 从左侧选择一个策略查看详情'}
                  </div>
                )}
              </div>
            </div>
          </article>

          <div id="live-operation-area" className="space-y-4">
            <div className="grid grid-cols-3 gap-3">
              <article className="col-span-2 card p-4 space-y-3">
                <div className="flex items-center justify-between">
                  <div className="text-sm text-dashboard-muted">交易控制面板</div>
                  <span className={`text-xs font-semibold ${liveReady ? 'text-dashboard-good' : 'text-dashboard-bad'}`}>
                    {liveReady ? 'ready' : 'not ready'}
                  </span>
                </div>

                {selectedStrategyId ? (
                  <div className="rounded-lg border border-sky-500/30 bg-sky-500/5 p-2">
                    <div className="text-xs text-sky-300">当前实盘策略</div>
                    <div className="text-sm font-medium">{selectedStrategy?.name || selectedStrategyId}</div>
                  </div>
                ) : null}

                <div className="space-y-2 text-sm">
                  <label className="block text-xs text-dashboard-muted">策略选择</label>
                  <select
                    value={selectedStrategyId}
                    onChange={(e) => setSelectedStrategyId(e.target.value)}
                    className="w-full rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2"
                  >
                    <option value="">手动交易(无策略)</option>
                    {strategies.map((s) => (
                      <option key={s.strategy_id} value={s.strategy_id}>
                        {s.strategy_id} | {s.name}
                      </option>
                    ))}
                  </select>

                  <label className="block text-xs text-dashboard-muted">Token ID</label>
                  <input
                    value={tokenId}
                    onChange={(e) => setTokenId(e.target.value)}
                    className="w-full rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2"
                    placeholder="输入 Token ID"
                  />

                  <div className="grid grid-cols-2 gap-2">
                    <button
                      onClick={() => setSide('BUY')}
                      className={`rounded-lg border px-3 py-2 ${side === 'BUY' ? 'border-emerald-400 bg-emerald-500/15 text-emerald-300' : 'border-dashboard-line bg-[#111827]'}`}
                    >
                      BUY
                    </button>
                    <button
                      onClick={() => setSide('SELL')}
                      className={`rounded-lg border px-3 py-2 ${side === 'SELL' ? 'border-rose-400 bg-rose-500/15 text-rose-300' : 'border-dashboard-line bg-[#111827]'}`}
                    >
                      SELL
                    </button>
                  </div>

                  <div className="grid grid-cols-2 gap-2">
                    <button
                      onClick={() => setOrderType('GTC')}
                      className={`rounded-lg border px-3 py-2 ${orderType === 'GTC' ? 'border-sky-400 bg-sky-500/15 text-sky-300' : 'border-dashboard-line bg-[#111827]'}`}
                    >
                      GTC
                    </button>
                    <button
                      onClick={() => setOrderType('FOK')}
                      className={`rounded-lg border px-3 py-2 ${orderType === 'FOK' ? 'border-sky-400 bg-sky-500/15 text-sky-300' : 'border-dashboard-line bg-[#111827]'}`}
                    >
                      FOK
                    </button>
                  </div>

                  {orderType === 'GTC' ? (
                    <div className="grid grid-cols-2 gap-2">
                      <div>
                        <label className="block text-xs text-dashboard-muted mb-1">价格</label>
                        <input
                          type="number"
                          step="0.0001"
                          value={price}
                          onChange={(e) => setPrice(asNumber(e.target.value, 0))}
                          className="w-full rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2"
                        />
                      </div>
                      <div>
                        <label className="block text-xs text-dashboard-muted mb-1">数量</label>
                        <input
                          type="number"
                          step="0.0001"
                          value={size}
                          onChange={(e) => setSize(asNumber(e.target.value, 0))}
                          className="w-full rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2"
                        />
                      </div>
                    </div>
                  ) : (
                    <div>
                      <label className="block text-xs text-dashboard-muted mb-1">金额(USDC)</label>
                      <input
                        type="number"
                        step="0.0001"
                        value={amount}
                        onChange={(e) => setAmount(asNumber(e.target.value, 0))}
                        className="w-full rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2"
                      />
                    </div>
                  )}

                  <label className="inline-flex items-center gap-2 text-xs text-dashboard-muted">
                    <input
                      type="checkbox"
                      checked={confirmLive}
                      onChange={(e) => setConfirmLive(e.target.checked)}
                    />
                    我确认这是实盘交易
                  </label>

                  <div className="grid grid-cols-2 gap-2">
                    <button
                      onClick={() => void placeLimitOrder()}
                      disabled={!canLimitOrder}
                      className="rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-emerald-300 disabled:opacity-40 disabled:cursor-not-allowed"
                    >
                      下限价单
                    </button>
                    <button
                      onClick={() => void placeMarketOrder()}
                      disabled={!canMarketOrder}
                      className="rounded-lg border border-sky-500/40 bg-sky-500/10 px-3 py-2 text-sky-300 disabled:opacity-40 disabled:cursor-not-allowed"
                    >
                      下市价单
                    </button>
                  </div>

                  <div>
                    <label className="block text-xs text-dashboard-muted mb-1">返回结果</label>
                    <pre className={`rounded-lg border px-3 py-2 text-xs whitespace-pre-wrap min-h-[110px] overflow-auto ${
                      resultTone === 'ok'
                        ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200'
                        : 'border-rose-500/40 bg-rose-500/10 text-rose-200'
                    }`}
                    >
                      {resultText || '-'}
                    </pre>
                  </div>
                </div>
              </article>

              <article className="col-span-1 card p-4">
                <div className="flex items-center justify-between mb-2">
                  <div className="text-sm text-dashboard-muted inline-flex items-center gap-1">
                    <Bot size={14} />
                    自动 Bot 控制
                  </div>
                  <div className="text-xs text-dashboard-muted">
                    {botStatus?.running ? `运行中 | tick: ${botStatus.tick}` : '已停止'}
                  </div>
                </div>
                <div className="space-y-2">
                  <input
                    value={botTokenId}
                    onChange={(e) => setBotTokenId(e.target.value)}
                    className="w-full rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2"
                    placeholder="Token ID"
                  />
                  <input
                    type="number"
                    value={botInterval}
                    onChange={(e) => setBotInterval(asNumber(e.target.value, 20))}
                    className="w-full rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2"
                    placeholder="间隔秒"
                  />
                  <div className="grid grid-cols-2 gap-2">
                    <button
                      onClick={() => void startBot()}
                      disabled={!liveReady || !confirmLive || !botTokenId.trim() || busyBot}
                      className="rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-emerald-300 disabled:opacity-40 disabled:cursor-not-allowed"
                    >
                      ▶ 启动Bot
                    </button>
                    <button
                      onClick={() => void stopBot()}
                      disabled={busyBot}
                      className="rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-rose-300 disabled:opacity-40 disabled:cursor-not-allowed"
                    >
                      ⏹ 停止Bot
                    </button>
                  </div>
                </div>
              </article>
            </div>

            <article className="card p-3 space-y-2">
              <div className="flex items-center justify-between">
                <div className="text-sm text-dashboard-muted">订单 & 成交（每 5 秒自动刷新）</div>
                <button
                  onClick={() => void cancelAllOrders()}
                  disabled={!confirmLive}
                  className="rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-1.5 text-xs text-rose-300 disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  全撤单
                </button>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div className="rounded-lg border border-dashboard-line bg-[#111827] p-2">
                  <div className="text-xs text-dashboard-muted mb-2">Open Orders</div>
                  {openOrdersDisabled ? (
                    <div className="rounded-lg border border-yellow-500/40 bg-yellow-500/10 px-2 py-2 text-xs text-yellow-200">
                      账户接口未启用: {openOrdersDisabled.reason || '-'}
                    </div>
                  ) : (
                    <div className="overflow-auto scroll-dark max-h-[280px]">
                      <table className="w-full text-xs">
                        <thead className="text-dashboard-muted">
                          <tr>
                            <th className="text-left px-1 py-1 font-medium">时间</th>
                            <th className="text-left px-1 py-1 font-medium">方向</th>
                            <th className="text-left px-1 py-1 font-medium">价格</th>
                            <th className="text-left px-1 py-1 font-medium">数量</th>
                            <th className="text-left px-1 py-1 font-medium">状态</th>
                          </tr>
                        </thead>
                        <tbody>
                          {openOrders.map((row, idx) => {
                            const timeRaw = String(row.time || row.timestamp || '');
                            const qty = row.original_size ?? row.size;
                            return (
                              <tr key={`ord-${idx}`} className="border-t border-dashboard-line">
                                <td className="px-1 py-1">{toLocalTime(timeRaw)}</td>
                                <td className="px-1 py-1">{String(row.side || '').toUpperCase()}</td>
                                <td className="px-1 py-1">{fmt4(row.price)}</td>
                                <td className="px-1 py-1">{fmt4(qty)}</td>
                                <td className="px-1 py-1">{row.status || '-'}</td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                      {openOrders.length === 0 ? <div className="text-center text-dashboard-muted py-6">暂无订单</div> : null}
                    </div>
                  )}
                </div>

                <div className="rounded-lg border border-dashboard-line bg-[#111827] p-2">
                  <div className="text-xs text-dashboard-muted mb-2">Recent Trades</div>
                  {recentTradesDisabled ? (
                    <div className="rounded-lg border border-yellow-500/40 bg-yellow-500/10 px-2 py-2 text-xs text-yellow-200">
                      账户接口未启用: {recentTradesDisabled.reason || '-'}
                    </div>
                  ) : (
                    <div className="overflow-auto scroll-dark max-h-[280px]">
                      <table className="w-full text-xs">
                        <thead className="text-dashboard-muted">
                          <tr>
                            <th className="text-left px-1 py-1 font-medium">时间</th>
                            <th className="text-left px-1 py-1 font-medium">方向</th>
                            <th className="text-left px-1 py-1 font-medium">价格</th>
                            <th className="text-left px-1 py-1 font-medium">数量</th>
                            <th className="text-left px-1 py-1 font-medium">市场/Token</th>
                          </tr>
                        </thead>
                        <tbody>
                          {recentTrades.map((row, idx) => {
                            const timeRaw = String(row.time || row.timestamp || '');
                            return (
                              <tr key={`trd-${idx}`} className="border-t border-dashboard-line">
                                <td className="px-1 py-1">{toLocalTime(timeRaw)}</td>
                                <td className="px-1 py-1">{String(row.side || '').toUpperCase()}</td>
                                <td className="px-1 py-1">{fmt4(row.price)}</td>
                                <td className="px-1 py-1">{fmt4(row.size)}</td>
                                <td className="px-1 py-1 max-w-[160px] truncate">{row.market || row.asset_id || row.token_id || '-'}</td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                      {recentTrades.length === 0 ? <div className="text-center text-dashboard-muted py-6">暂无成交</div> : null}
                    </div>
                  )}
                </div>
              </div>
            </article>
          </div>
        </section>
      ) : null}

      {activeTab === 'tab3' ? (
        <section className="space-y-3">
          <article className="card p-3 space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              <input
                value={marketSearch}
                onChange={(e) => setMarketSearch(e.target.value)}
                className="min-w-[280px] flex-1 rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 text-sm"
                placeholder="搜索市场名称 / 英文名 / market_id"
              />
              <select
                value={marketSort}
                onChange={(e) => setMarketSort(e.target.value as MarketSortKey)}
                className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 text-sm"
              >
                <option value="volume">按成交量</option>
                <option value="spread">按Spread</option>
                <option value="depth">按深度</option>
              </select>
              <button
                onClick={() => void loadMarketMonitor()}
                className="rounded-lg border border-dashboard-line bg-[#111827] px-3 py-2 hover:border-[#4b5563] inline-flex items-center gap-1 text-sm"
              >
                <RefreshCw size={14} />
                刷新
              </button>
              <div className="ml-auto text-xs text-dashboard-muted">
                {marketLoading ? '加载中...' : `共 ${filteredSortedMarkets.length} 条`}
              </div>
            </div>

            {marketError ? (
              <div className="rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
                拉取市场失败: {marketError}
              </div>
            ) : null}

            <div className="overflow-auto scroll-dark max-h-[620px]">
              <table className="w-full text-sm min-w-[1380px]">
                <thead className="text-dashboard-muted bg-[#111827]">
                  <tr>
                    <th className="text-left px-2 py-2 font-medium">#</th>
                    <th className="text-left px-2 py-2 font-medium">市场名称</th>
                    <th className="text-left px-2 py-2 font-medium">Yes价</th>
                    <th className="text-left px-2 py-2 font-medium">No价</th>
                    <th className="text-left px-2 py-2 font-medium">Spread%</th>
                    <th className="text-left px-2 py-2 font-medium">24h成交量</th>
                    <th className="text-left px-2 py-2 font-medium">深度(USDC)</th>
                    <th className="text-left px-2 py-2 font-medium">Yes+No</th>
                    <th className="text-left px-2 py-2 font-medium">机会</th>
                    <th className="text-left px-2 py-2 font-medium">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {marketPageRows.flatMap((row, idx) => {
                    const noMaybe = Number((row as { no_mid?: unknown }).no_mid);
                    const yesMaybe = Number((row as { yes_mid?: unknown }).yes_mid);
                    const yesPrice = Number.isFinite(yesMaybe) ? yesMaybe : Number.NaN;
                    const noPrice = Number.isFinite(noMaybe) ? noMaybe : (Number.isFinite(yesPrice) ? (1 - yesPrice) : Number.NaN);
                    const spreadPct = asNumber(row.spread_pct, 0);
                    const sum = asNumber(row.yes_no_sum, 0);
                    const sumDeviation = Math.abs(sum - 1);
                    const isExpanded = expandedMarketId === row.market_id;
                    const tokenRows = tokenOptionsByMarket[row.market_id] || [];
                    const expandError = marketTokenError[row.market_id] || '';
                    const rowNo = (marketPage - 1) * 20 + idx + 1;

                    const mainRow = (
                      <tr key={`market-${row.market_id}`} className="border-t border-dashboard-line">
                        <td className="px-2 py-2 text-dashboard-muted">{rowNo}</td>
                        <td className="px-2 py-2">
                          <div className="max-w-[420px] truncate" title={`${row.name || ''}${row.name_en ? ` | ${row.name_en}` : ''}`}>
                            {row.name || row.name_en || row.market_id}
                          </div>
                          <div className="text-[11px] text-dashboard-muted font-mono truncate max-w-[420px]" title={row.market_id}>
                            {row.market_id}
                          </div>
                        </td>
                        <td className="px-2 py-2">{Number.isFinite(yesPrice) ? fmt4(yesPrice) : '-'}</td>
                        <td className="px-2 py-2">{Number.isFinite(noPrice) ? fmt4(noPrice) : '-'}</td>
                        <td className={`px-2 py-2 ${spreadPct >= 4 ? 'text-dashboard-good font-semibold' : ''}`}>
                          {fmt4(spreadPct)}%
                        </td>
                        <td className="px-2 py-2">{fmtAmount(row.volume_24h)}</td>
                        <td className="px-2 py-2">{fmtAmount(row.depth_usdc)}</td>
                        <td className={`px-2 py-2 ${sumDeviation >= 0.04 ? 'text-yellow-300 font-semibold' : ''}`}>
                          {fmt4(sum)}
                        </td>
                        <td className="px-2 py-2">
                          <div className="flex items-center gap-1">
                            {row.mm_opportunity ? (
                              <span className="rounded-md border border-sky-500/40 bg-sky-500/10 px-1.5 py-0.5 text-[11px] text-sky-300">MM</span>
                            ) : null}
                            {row.arb_opportunity ? (
                              <span className="rounded-md border border-emerald-500/40 bg-emerald-500/10 px-1.5 py-0.5 text-[11px] text-emerald-300">ARB</span>
                            ) : null}
                            {!row.mm_opportunity && !row.arb_opportunity ? (
                              <span className="text-dashboard-muted text-xs">-</span>
                            ) : null}
                          </div>
                        </td>
                        <td className="px-2 py-2">
                          <button
                            onClick={() => void onSelectMarket(row.market_id)}
                            className="rounded-md border border-dashboard-line bg-[#111827] px-2 py-1 hover:border-[#4b5563] text-xs"
                          >
                            {isExpanded ? '收起' : '选中交易'}
                          </button>
                        </td>
                      </tr>
                    );

                    if (!isExpanded) return [mainRow];

                    const expandedRow = (
                      <tr key={`expand-${row.market_id}`} className="border-t border-dashboard-line/60 bg-[#111827]/60">
                        <td className="px-2 py-2" colSpan={10}>
                          <div className="space-y-2">
                            <div className="text-xs text-dashboard-muted">选择 Token 后将自动切换到 Tab 2 并填入 token_id。</div>
                            {marketTokenLoadingId === row.market_id ? (
                              <div className="text-xs text-dashboard-muted">正在拉取 {row.market_id} 的 token 列表...</div>
                            ) : null}
                            {expandError ? (
                              <div className="rounded-lg border border-yellow-500/40 bg-yellow-500/10 px-2 py-2 text-xs text-yellow-200">
                                {expandError}
                              </div>
                            ) : null}
                            {!expandError && marketTokenLoadingId !== row.market_id && tokenRows.length === 0 ? (
                              <div className="text-xs text-dashboard-muted">未返回可用 token_id，请手动在 Tab 2 输入。</div>
                            ) : null}
                            {tokenRows.length > 0 ? (
                              <div className="grid grid-cols-2 gap-2">
                                {tokenRows.map((opt) => (
                                  <button
                                    key={`${row.market_id}-${opt.token_id}`}
                                    onClick={() => useTokenFromMarket(opt.token_id)}
                                    className="rounded-lg border border-dashboard-line bg-[#0f172a] px-3 py-2 text-left hover:border-sky-400"
                                  >
                                    <div className="text-xs text-sky-300">{opt.label}</div>
                                    <div className="font-mono text-xs break-all">{opt.token_id}</div>
                                    {opt.price !== null && opt.price !== undefined ? (
                                      <div className="text-[11px] text-dashboard-muted">价格 {fmt4(opt.price)}</div>
                                    ) : null}
                                  </button>
                                ))}
                              </div>
                            ) : null}
                          </div>
                        </td>
                      </tr>
                    );

                    return [mainRow, expandedRow];
                  })}
                </tbody>
              </table>
              {!marketLoading && marketPageRows.length === 0 ? (
                <div className="text-center py-10 text-dashboard-muted">暂无市场数据</div>
              ) : null}
            </div>

            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="text-xs text-dashboard-muted">
                第 {marketPage} / {marketTotalPages} 页，共 {filteredSortedMarkets.length} 条（每页 20 条）
              </div>
              <div className="flex items-center gap-1">
                <button
                  onClick={() => setMarketPage((p) => Math.max(1, p - 1))}
                  disabled={marketPage <= 1}
                  className="rounded-md border border-dashboard-line bg-[#111827] px-2 py-1 text-xs disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  上一页
                </button>
                {marketPageButtons.map((p) => (
                  <button
                    key={`pg-${p}`}
                    onClick={() => setMarketPage(p)}
                    className={`rounded-md border px-2 py-1 text-xs ${
                      p === marketPage
                        ? 'border-sky-400 bg-sky-500/15 text-sky-300'
                        : 'border-dashboard-line bg-[#111827] text-dashboard-text'
                    }`}
                  >
                    {p}
                  </button>
                ))}
                <button
                  onClick={() => setMarketPage((p) => Math.min(marketTotalPages, p + 1))}
                  disabled={marketPage >= marketTotalPages}
                  className="rounded-md border border-dashboard-line bg-[#111827] px-2 py-1 text-xs disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  下一页
                </button>
              </div>
            </div>
          </article>
        </section>
      ) : null}
    </div>
  );
}
