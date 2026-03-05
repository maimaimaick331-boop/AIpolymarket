const API_BASE = (import.meta.env.VITE_API_BASE as string | undefined)?.trim() || 'http://127.0.0.1:8780/api';

export async function apiGet<T>(path: string): Promise<T> {
  const resp = await fetch(`${API_BASE}${path}`, { headers: { Accept: 'application/json' } });
  const text = await resp.text();
  let body: unknown;
  try {
    body = text ? JSON.parse(text) : {};
  } catch {
    body = text;
  }
  if (!resp.ok) {
    const message = typeof body === 'object' && body !== null && 'detail' in body
      ? JSON.stringify((body as { detail: unknown }).detail)
      : `HTTP ${resp.status}`;
    throw new Error(message);
  }
  return body as T;
}

export async function apiPost<T>(path: string, payload?: unknown): Promise<T> {
  const resp = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
    },
    body: payload === undefined ? undefined : JSON.stringify(payload),
  });
  const text = await resp.text();
  let body: unknown;
  try {
    body = text ? JSON.parse(text) : {};
  } catch {
    body = text;
  }
  if (!resp.ok) {
    const message = typeof body === 'object' && body !== null && 'detail' in body
      ? JSON.stringify((body as { detail: unknown }).detail)
      : `HTTP ${resp.status}`;
    throw new Error(message);
  }
  return body as T;
}

export function toLocalTime(iso: string): string {
  if (!iso) return '-';
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) return '-';
  return dt.toLocaleString('zh-CN', { hour12: false });
}
