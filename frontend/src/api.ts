// Single source of truth for every backend call. VITE_API_URL comes from frontend/.env.development (local), frontend/.env.production (build) or the
// Vercel project's Environment Variables, which override the file. The localhost fallback exists for the dev server ONLY, so a production build can never silently target localhost.
const BASE: string = String((import.meta as any).env.VITE_API_URL || ((import.meta as any).env.DEV ? 'http://localhost:8000/api' : '')).replace(/\/+$/, '')
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
// freshHours: only issues GitHub updated within this many hours qualify; 0 = no window (historical research); undefined = the server default (48).
export async function searchGithub(keyword: string, limit = 25, freshHours?: number): Promise<{ data?: any; error?: string }> {
  try {
    const r = await fetch(BASE + '/github/search', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ keyword, limit, ...(freshHours === undefined ? {} : { fresh_hours: freshHours }) }), signal: AbortSignal.timeout(120000) })
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

// "Mark as commented": you posted a comment on this GitHub issue yourself and are telling Nudge so. Local bookkeeping only: the backend records it in
// its own database and never contacts GitHub. Surfaces the server's message on failure instead of swallowing it (api() returns null for every error).
export async function markCommented(postId: number): Promise<{ data?: any; error?: string }> {
  try {
    const r = await fetch(BASE + `/posts/${postId}/mark-commented`, { method: 'POST', signal: AbortSignal.timeout(15000) })
    const j = await r.json().catch(() => null)
    if (!r.ok) return { error: typeof j?.detail === 'string' ? j.detail : `Could not save that (HTTP ${r.status}).` }
    return { data: j }
  } catch (e: any) {
    return { error: e?.name === 'TimeoutError' ? 'Saving took too long. Try again.' : `Cannot reach the Nudge backend at ${BASE}. Is it running?` }
  }
}

// ---- Conversations (GitHub reply tracking) --------------------------------------------------------------------------------------------------------
// Shared by the two calls below. Like markCommented/searchGithub it surfaces the server's own message (rate limit, bad token, GitHub down, "a check is
// already running") instead of swallowing it the way api() does. It only talks to Nudge's own backend; the backend is what reads GitHub (GET only).
async function post(path: string, timeoutMs: number, slow: string, failed: string): Promise<{ data?: any; error?: string }> {
  try {
    const r = await fetch(BASE + path, { method: 'POST', signal: AbortSignal.timeout(timeoutMs) })
    const j = await r.json().catch(() => null)
    if (!r.ok) {
      const d = j?.detail
      return { error: typeof d === 'string' ? d : `${failed} (HTTP ${r.status}).` }
    }
    return { data: j }
  } catch (e: any) {
    return { error: e?.name === 'TimeoutError' ? slow : `Cannot reach the Nudge backend at ${BASE}. Is it running?` }
  }
}

// "Check for replies": manual only, never polled. The backend reads comments on issues you commented on through GitHub's REST API and saves new ones.
// Resolves to { data: { results: { new_replies, new_mentions, new_comments, unread_total, warnings, rate_limited, incomplete, ... } } }.
// Can take a while (one request per tracked issue), so the timeout is generous.
export const checkReplies = () =>
  post('/conversations/check-replies', 120000, 'The reply check took too long. Try again in a moment.', 'The reply check failed')

// Mark one conversation as read. The backend clears `unread` and persists it, so it survives a restart. Resolves to { data: <updated conversation> }.
export const markConversationRead = (conversationId: number) =>
  post(`/conversations/${conversationId}/read`, 15000, 'Saving took too long. Try again.', 'Could not mark that conversation as read')