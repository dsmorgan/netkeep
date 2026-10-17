import { DISCONNECTED_EVENT, api } from '@/api/client'

export { DISCONNECTED_EVENT }

/**
 * Connecting this browser to the local API (#618, ADR 0013).
 *
 * The credential is an `HttpOnly` cookie the server sets, so no script here
 * ever sees it. `GET /api/v1/session` says whether the browser has it, and
 * whether the first-browser claim is still open.
 */
export type SessionState = 'connected' | 'disconnected'

let inFlight: Promise<SessionState> | null = null

/**
 * Whether this browser is connected, claiming the session first when no
 * browser has yet. Concurrent callers share one check, so React's StrictMode
 * double effect never sends two claims (the second would lose the race and
 * read as "another browser connected").
 *
 * A server that can't be reached reads as connected: the app's own banners
 * already say the server is down, and the connect page would only mislead.
 */
export function checkSession(): Promise<SessionState> {
  inFlight ??= resolveSession().finally(() => {
    inFlight = null
  })
  return inFlight
}

async function resolveSession(): Promise<SessionState> {
  try {
    const { data } = await api.GET('/api/v1/session')
    if (!data) return 'connected'
    if (data.connected) return 'connected'
    if (!data.claimable) return 'disconnected'
    const { response } = await api.POST('/api/v1/session/claim')
    return response.ok ? 'connected' : 'disconnected'
  } catch {
    return 'connected'
  }
}
