import { DISCONNECTED_EVENT, api } from '@/api/client'

export { DISCONNECTED_EVENT }

/**
 * Connecting this browser to the local API (#618, ADR 0013).
 *
 * The credential is an `HttpOnly` cookie the server sets, so no script here
 * ever sees it. `GET /api/v1/session` says whether the browser has it (and
 * sets it again, so it never expires), whether the first-browser claim is
 * still open, and whether the key is in the Keychain or only in memory.
 */
export type SessionInfo = {
  status: 'connected' | 'disconnected'
  /** False when the Keychain refused the key and it lives in memory until netkeeper stops. */
  persisted: boolean
  /** Another browser or program used the first-browser claim. */
  claimedElsewhere: boolean
  /** The `netkeeper open` command line that makes a link this server accepts. */
  openCommand: string
}

const FALLBACK_COMMAND = 'netkeeper open'

let inFlight: Promise<SessionInfo> | null = null

/**
 * Whether this browser is connected, claiming the session first when no
 * browser has yet. Concurrent callers share one check, so React's StrictMode
 * double effect never sends two claims (the second would lose the race and
 * read as "another browser connected").
 *
 * A server that can't be reached reads as connected: the app's own banners
 * already say the server is down, and the connect page would only mislead.
 */
export function checkSession(): Promise<SessionInfo> {
  inFlight ??= resolveSession().finally(() => {
    inFlight = null
  })
  return inFlight
}

async function resolveSession(): Promise<SessionInfo> {
  const unreachable: SessionInfo = {
    status: 'connected',
    persisted: true,
    claimedElsewhere: false,
    openCommand: FALLBACK_COMMAND,
  }
  try {
    const { data } = await api.GET('/api/v1/session')
    if (!data) return unreachable
    const info = {
      persisted: data.persisted,
      openCommand: data.open_command,
      claimedElsewhere: false,
    }
    if (data.connected) return { ...info, status: 'connected' }
    if (!data.claimable) return { ...info, status: 'disconnected', claimedElsewhere: true }
    const { response } = await api.POST('/api/v1/session/claim')
    return response.ok
      ? { ...info, status: 'connected' }
      : { ...info, status: 'disconnected', claimedElsewhere: response.status === 403 }
  } catch {
    return unreachable
  }
}
