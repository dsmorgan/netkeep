import createClient from 'openapi-fetch'

import type { paths } from './schema'

/**
 * The one HTTP client for the backend.
 *
 * `baseUrl` stays empty so requests resolve against the page origin: behind
 * the Vite dev proxy (`/api` → 127.0.0.1:8000) and when the backend serves
 * `dist/` itself. `X-Netkeeper-Client` is the CSRF marker the backend requires
 * on state-changing requests (spec 14.2); sending it on every request keeps
 * call sites uniform.
 */
export const api = createClient<paths>({
  baseUrl: '',
  headers: { 'X-Netkeeper-Client': '1' },
})

/** Fired on `window` when a call comes back 401 (see below). */
export const DISCONNECTED_EVENT = 'netkeeper:disconnected'

/**
 * A 401 means this browser isn't connected to the local API (#618): its
 * session cookie is gone or was made from an older key. The session gate
 * listens for this event and shows the connect page.
 */
api.use({
  onResponse({ response }) {
    if (response.status === 401) window.dispatchEvent(new Event(DISCONNECTED_EVENT))
    return undefined
  },
})

/** Response shapes derived from the paths, so schema renames in the export do not ripple. */
export type Health = paths['/api/v1/health']['get']['responses'][200]['content']['application/json']
export type Me = paths['/api/v1/me']['get']['responses'][200]['content']['application/json']
