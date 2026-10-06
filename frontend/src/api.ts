const BASE = (import.meta as any).env?.VITE_API_URL ?? 'http://localhost:8000/api'
// Returns parsed JSON, or null if the backend is unreachable / errors (UI then keeps its built-in mock data).
export async function api(path: string, method = 'GET', body?: unknown): Promise<any> {
  try {
    const r = await fetch(BASE + path, { method, headers: body ? { 'Content-Type': 'application/json' } : undefined, body: body ? JSON.stringify(body) : undefined, signal: AbortSignal.timeout(4000) })
    return r.ok ? await r.json() : null
  } catch { return null }
}
export const fetchBootstrap = () => api('/bootstrap')

// Manual GitHub keyword hunt. Unlike api(), this surfaces the server's error message (rate limit, GitHub down, bad keyword) instead of swallowing it,
// and waits long enough for a ~500-issue search (several paged GitHub requests) to finish.
export async function searchGithub(keyword: string, limit = 25): Promise<{ data?: any; error?: string }> {
  try {
    const r = await fetch(BASE + '/github/search', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ keyword, limit }), signal: AbortSignal.timeout(120000) })
    const j = await r.json().catch(() => null)
    if (!r.ok) {
      const d = j?.detail
      return { error: typeof d === 'string' ? d : Array.isArray(d) ? 'Invalid search: ' + d.map((x: any) => x.msg).join('; ') : `Search failed (HTTP ${r.status}).` }
    }
    return { data: j }
  } catch (e: any) {
    return { error: e?.name === 'TimeoutError' ? 'The search took too long. Try again.' : `Cannot reach the Nudge backend at ${BASE}. Is it running?` }
  }
}