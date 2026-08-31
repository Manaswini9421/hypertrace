// Typed client for the HyperTrace API (services/api-bff). Types here mirror
// the FastAPI response models — keep them in sync when endpoints change.

export interface SummaryEntry {
  service: string
  cost_per_hour: number
}

export interface DashboardSummary {
  total_cost_per_hour: number
  top_services: SummaryEntry[]
}

export interface CostPoint {
  time: string
  cost_per_hour: number
}

export interface Anomaly {
  id: string
  service: string
  score: number
  classification: string
  evidence: Record<string, unknown>
  status: string
  created_at: string
}

export interface Action {
  id: string
  anomaly_id: string | null
  action_type: string
  executed_at: string
  result: string
  rollback_ref: string | null
}

export interface Policy {
  id: string
  org_id: string
  rule_dsl: Record<string, unknown>
  scope: string
  action: string
  priority: number
}

const TOKEN_KEY = 'hypertrace.token'
const ROLE_KEY = 'hypertrace.role'

export const auth = {
  token: () => localStorage.getItem(TOKEN_KEY),
  role: () => localStorage.getItem(ROLE_KEY),
  clear: () => {
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(ROLE_KEY)
  },
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = auth.token()
  const res = await fetch(path, {
    ...init,
    headers: {
      ...(init.body ? { 'Content-Type': 'application/json' } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...init.headers,
    },
  })
  if (res.status === 401) {
    auth.clear()
    window.location.reload()
    throw new Error('Session expired')
  }
  if (!res.ok) {
    throw new Error(`${res.status} ${await res.text()}`)
  }
  return res.status === 204 ? (undefined as T) : ((await res.json()) as T)
}

export async function login(username: string, password: string): Promise<string> {
  const body = new URLSearchParams({ username, password })
  const res = await fetch('/api/v1/auth/login', { method: 'POST', body })
  if (!res.ok) throw new Error('Invalid username or password')
  const data = (await res.json()) as { access_token: string }
  localStorage.setItem(TOKEN_KEY, data.access_token)
  // Read the role straight out of the JWT payload so the UI knows which
  // controls to show. This is convenience only — the backend re-checks the
  // role on every write, so a tampered value here grants nothing.
  const payload = JSON.parse(atob(data.access_token.split('.')[1]))
  localStorage.setItem(ROLE_KEY, payload.role)
  return payload.role
}

export const api = {
  summary: () => request<DashboardSummary>('/api/v1/dashboard/summary'),
  serviceCost: (service: string, hours = 1) =>
    request<CostPoint[]>(`/api/v1/services/${encodeURIComponent(service)}/cost?range_hours=${hours}`),
  anomalies: (limit = 50) => request<Anomaly[]>(`/api/v1/anomalies?limit=${limit}`),
  actions: (limit = 50) => request<Action[]>(`/api/v1/actions?limit=${limit}`),
  policies: () => request<Policy[]>('/api/v1/policies'),
  createPolicy: (p: Omit<Policy, 'id'>) =>
    request<Policy>('/api/v1/policies', { method: 'POST', body: JSON.stringify(p) }),
  deletePolicy: (id: string) => request<void>(`/api/v1/policies/${id}`, { method: 'DELETE' }),
  approve: (anomalyId: string) =>
    request<{ action_id: string; status: string }>(`/api/v1/actions/${anomalyId}/approve`, { method: 'POST' }),
  rollback: (anomalyId: string) =>
    request<{ action_id: string; status: string }>(`/api/v1/actions/${anomalyId}/rollback`, { method: 'POST' }),
}

export function openAlertStream(onAlert: (a: Anomaly) => void): WebSocket {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  const ws = new WebSocket(`${proto}://${window.location.host}/api/v1/stream/alerts?token=${auth.token()}`)
  ws.onmessage = (e) => onAlert(JSON.parse(e.data) as Anomaly)
  return ws
}
