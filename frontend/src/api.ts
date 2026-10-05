const BASE = (import.meta as any).env?.VITE_API_URL ?? 'http://localhost:8000/api'
// Returns parsed JSON, or null if the backend is unreachable / errors (UI then keeps its built-in mock data).
export async function api(path: string, method = 'GET', body?: unknown): Promise<any> {
  try {
    const r = await fetch(BASE + path, { method, headers: body ? { 'Content-Type': 'application/json' } : undefined, body: body ? JSON.stringify(body) : undefined, signal: AbortSignal.timeout(4000) })
    return r.ok ? await r.json() : null
  } catch { return null }
}
export const fetchBootstrap = () => api('/bootstrap')
