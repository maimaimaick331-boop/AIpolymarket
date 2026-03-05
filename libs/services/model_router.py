from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json
import random
from urllib.request import Request, urlopen

from libs.services.openclaw_client import OpenClawClient


@dataclass
class ModelProvider:
    provider_id: str
    name: str
    endpoint: str
    adapter: str = 'openclaw_compatible'
    model: str = ''
    enabled: bool = True
    weight: float = 1.0
    priority: int = 100
    company: str = 'custom'
    api_key: str = ''
    extra_headers: dict[str, str] | None = None


@dataclass
class ModelAllocation:
    mode: str = 'weighted'  # weighted | priority
    providers: list[ModelProvider] | None = None


def company_presets() -> list[dict[str, Any]]:
    return [
        {
            'company': 'local',
            'name': '本地模型',
            'adapter': 'openai_compatible',
            'default_endpoint': 'http://127.0.0.1:11434/v1/chat/completions',
            'requires_api_key': False,
            'supports_catalog': True,
            'docs_url': '',
        },
        {
            'company': 'openrouter',
            'name': 'OpenRouter',
            'adapter': 'openai_compatible',
            'default_endpoint': 'https://openrouter.ai/api/v1/chat/completions',
            'requires_api_key': True,
            'supports_catalog': True,
            'docs_url': 'https://openrouter.ai/docs/api-reference/overview',
        },
        {
            'company': 'yunwu',
            'name': 'Yunwu',
            'adapter': 'openai_compatible',
            'default_endpoint': 'https://api.yunwu.ai/v1/chat/completions',
            'requires_api_key': True,
            'supports_catalog': True,
            'docs_url': 'https://yunwu.ai/',
        },
        {
            'company': 'custom',
            'name': '自定义',
            'adapter': 'openai_compatible',
            'default_endpoint': '',
            'requires_api_key': False,
            'supports_catalog': True,
            'docs_url': '',
        },
    ]


def infer_company(endpoint: str, adapter: str = '') -> str:
    ep = (endpoint or '').strip().lower()
    ad = (adapter or '').strip().lower()
    if ad == 'openclaw_compatible' or ep.endswith('/generate'):
        return 'local'
    if 'openrouter.ai' in ep:
        return 'openrouter'
    if 'yunwu.ai' in ep:
        return 'yunwu'
    if '127.0.0.1' in ep or 'localhost' in ep:
        return 'local'
    return 'custom'


def _norm_base(url: str) -> str:
    u = (url or '').strip().rstrip('/')
    if not u:
        return ''
    for suffix in ['/generate', '/v1/chat/completions', '/v1/models', '/health']:
        if u.endswith(suffix):
            return u[: -len(suffix)]
    return u


def normalize_provider_endpoint(endpoint: str, adapter: str = 'openai_compatible', company: str = 'custom') -> str:
    ep = (endpoint or '').strip()
    ad = (adapter or '').strip().lower()
    co = (company or '').strip().lower()
    if ad == 'openclaw_compatible':
        if not ep:
            return 'http://127.0.0.1:8000/generate'
        if ep.endswith('/generate'):
            return ep
        return f'{ep.rstrip("/")}/generate'

    if not ep:
        for row in company_presets():
            if row.get('company') == co:
                default_ep = str(row.get('default_endpoint', '')).strip()
                if default_ep:
                    ep = default_ep
                    break

    if not ep:
        return ''

    ep = ep.rstrip('/')
    if ep.endswith('/v1/models'):
        return f'{ep[:-10]}/v1/chat/completions'
    if ep.endswith('/models'):
        return f'{ep[:-7]}/chat/completions'
    if ep.endswith('/v1/chat/completions') or ep.endswith('/chat/completions'):
        return ep
    if ep.endswith('/v1'):
        return f'{ep}/chat/completions'
    return f'{ep}/v1/chat/completions'


def models_endpoint_from_chat(endpoint: str) -> str:
    ep = (endpoint or '').strip().rstrip('/')
    if not ep:
        return ''
    if ep.endswith('/v1/chat/completions'):
        return f'{ep[:-20]}/v1/models'
    if ep.endswith('/chat/completions'):
        return f'{ep[:-17]}/models'
    if ep.endswith('/v1/models') or ep.endswith('/models'):
        return ep
    if ep.endswith('/v1'):
        return f'{ep}/models'
    return f'{ep}/v1/models'


def normalize_extra_headers(headers: dict[str, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(headers, dict):
        return out
    for k, v in headers.items():
        kk = str(k or '').strip()
        vv = str(v or '').strip()
        if kk and vv:
            out[kk] = vv
    return out


def fetch_openai_compatible_models(
    endpoint: str,
    api_key: str = '',
    timeout_sec: float = 15.0,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    models_ep = models_endpoint_from_chat(endpoint)
    if not models_ep:
        raise ValueError('endpoint 不能为空')

    headers: dict[str, str] = {'Accept': 'application/json'}
    key = (api_key or '').strip()
    if key:
        headers['Authorization'] = f'Bearer {key}'
    for k, v in normalize_extra_headers(extra_headers).items():
        headers[k] = v

    req = Request(models_ep, headers=headers, method='GET')
    with urlopen(req, timeout=timeout_sec) as resp:
        payload = json.loads(resp.read().decode('utf-8'))

    rows = payload.get('data', []) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        rows = []

    out_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        mid = str(row.get('id', '')).strip()
        if not mid:
            continue
        pricing = row.get('pricing', {}) if isinstance(row.get('pricing'), dict) else {}
        out_rows.append(
            {
                'id': mid,
                'name': str(row.get('name', '') or mid),
                'context_length': row.get('context_length', row.get('max_context_length', None)),
                'prompt_price': pricing.get('prompt', ''),
                'completion_price': pricing.get('completion', ''),
                'raw': row,
            }
        )
    out_rows.sort(key=lambda x: str(x.get('id', '')))
    return {'count': len(out_rows), 'rows': out_rows, 'models_endpoint': models_ep}


class ModelRouterStore:
    def __init__(self, store_dir: Path) -> None:
        self.store_dir = store_dir
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.store_dir / 'model_allocation.json'

    def load(self) -> ModelAllocation:
        if not self.path.exists():
            return ModelAllocation(mode='weighted', providers=[])
        try:
            payload = json.loads(self.path.read_text(encoding='utf-8'))
        except Exception:
            return ModelAllocation(mode='weighted', providers=[])

        mode = str(payload.get('mode', 'weighted')).lower()
        rows = payload.get('providers', [])
        providers: list[ModelProvider] = []
        if isinstance(rows, list):
            for r in rows:
                if not isinstance(r, dict):
                    continue
                providers.append(
                    ModelProvider(
                        provider_id=str(r.get('provider_id', '')),
                        name=str(r.get('name', '')),
                        endpoint=str(r.get('endpoint', '')),
                        adapter=str(r.get('adapter', 'openclaw_compatible')),
                        model=str(r.get('model', '')),
                        enabled=bool(r.get('enabled', True)),
                        weight=float(r.get('weight', 1.0)),
                        priority=int(r.get('priority', 100)),
                        company=str(r.get('company', infer_company(str(r.get('endpoint', '')), str(r.get('adapter', ''))))),
                        api_key=str(r.get('api_key', '')),
                        extra_headers=normalize_extra_headers(r.get('extra_headers', {})),
                    )
                )
        return ModelAllocation(mode=mode, providers=providers)

    def save(self, cfg: ModelAllocation) -> None:
        out = {
            'updated_at_utc': datetime.now(timezone.utc).isoformat(),
            'mode': cfg.mode,
            'providers': [asdict(x) for x in (cfg.providers or [])],
        }
        self.path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')


def discover_local_providers(extra_endpoints: list[str] | None = None) -> list[dict[str, Any]]:
    def _probe_openclaw(ep: str) -> dict[str, Any] | None:
        h = OpenClawClient(endpoint=ep, timeout_sec=3, retries=0).health_check()
        if not h.ok:
            return None
        return {
            'endpoint': ep,
            'adapter': 'openclaw_compatible',
            'company': 'local',
            'health': asdict(h),
        }

    def _probe_openai(base: str) -> dict[str, Any] | None:
        models_ep = f'{base}/v1/models'
        try:
            req = Request(models_ep, headers={'Accept': 'application/json'}, method='GET')
            with urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            model_name = ''
            if isinstance(data, dict):
                rows = data.get('data', [])
                if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                    model_name = str(rows[0].get('id', ''))
            return {
                'endpoint': f'{base}/v1/chat/completions',
                'adapter': 'openai_compatible',
                'company': 'local',
                'model': model_name,
                'health': {'ok': True, 'status': 'ok', 'latency_ms': 0, 'detail': 'v1/models'},
            }
        except Exception:
            return None

    bases: list[str] = []
    for ep in extra_endpoints or []:
        b = _norm_base(ep)
        if b:
            bases.append(b)

    for p in [11434, 1234, 8000, 8788, 9000, 3000]:
        bases.append(f'http://127.0.0.1:{p}')

    seen = set()
    out: list[dict[str, Any]] = []
    for b in bases:
        if b in seen:
            continue
        seen.add(b)
        openclaw_ep = f'{b}/generate'
        hit = _probe_openclaw(openclaw_ep)
        if hit:
            out.append(
                {
                    'provider_id': f'local-{len(out)+1:03d}',
                    'name': f'LocalOpenClaw-{len(out)+1}',
                    **hit,
                    'enabled': True,
                    'weight': 1.0,
                    'priority': 100 + len(out),
                }
            )

        openai_hit = _probe_openai(b)
        if openai_hit:
            out.append(
                {
                    'provider_id': f'local-{len(out)+1:03d}',
                    'name': f'LocalOpenAI-{len(out)+1}',
                    **openai_hit,
                    'enabled': True,
                    'weight': 1.0,
                    'priority': 100 + len(out),
                }
            )
    return out


def choose_provider(cfg: ModelAllocation, provider_id: str = '') -> ModelProvider | None:
    providers = [p for p in (cfg.providers or []) if p.enabled and p.endpoint.strip()]
    if not providers:
        return None

    if provider_id:
        for p in providers:
            if p.provider_id == provider_id:
                return p
        return None

    mode = cfg.mode.lower().strip()
    if mode == 'priority':
        providers.sort(key=lambda x: x.priority)
        return providers[0]

    # weighted
    total = sum(max(0.0, p.weight) for p in providers)
    if total <= 1e-12:
        return random.choice(providers)
    pick = random.random() * total
    run = 0.0
    for p in providers:
        run += max(0.0, p.weight)
        if pick <= run:
            return p
    return providers[-1]
