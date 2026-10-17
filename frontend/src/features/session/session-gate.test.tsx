import { render, screen, waitFor } from '@testing-library/react'
import { StrictMode } from 'react'
import { afterEach, describe, expect, it } from 'vitest'

import { api } from '@/api/client'
import { jsonResponse, mockFetch } from '@/test/fetch'

import { SessionGate } from './session-gate'

type Session = { connected: boolean; claimable: boolean }

/** A fake server: the session state, the claim's answer, and every request seen. */
function serve(session: Session, claimStatus = 204) {
  const seen: string[] = []
  mockFetch((request) => {
    const { pathname } = new URL(request.url)
    seen.push(`${request.method} ${pathname}`)
    if (pathname === '/api/v1/session') return jsonResponse(session)
    if (pathname === '/api/v1/session/claim') {
      if (claimStatus === 204) {
        session = { connected: true, claimable: false }
        return new Response(null, { status: 204 })
      }
      return jsonResponse({ detail: 'another browser already connected' }, claimStatus)
    }
    if (pathname === '/api/v1/me') {
      return session.connected
        ? jsonResponse({ id: 1 })
        : jsonResponse({ detail: 'not connected', rule: 'local-key' }, 401)
    }
    return jsonResponse({ detail: 'Not Found' }, 404)
  })
  return {
    seen,
    disconnect: () => {
      session = { connected: false, claimable: false }
    },
  }
}

function renderGate() {
  return render(
    <StrictMode>
      <SessionGate>
        <p>the app</p>
      </SessionGate>
    </StrictMode>,
  )
}

afterEach(() => {
  window.history.replaceState(null, '', '/')
})

describe('SessionGate', () => {
  it('renders the app for a connected browser without claiming anything', async () => {
    const server = serve({ connected: true, claimable: false })
    renderGate()
    expect(await screen.findByText('the app')).toBeInTheDocument()
    expect(server.seen).not.toContain('POST /api/v1/session/claim')
  })

  it('claims the session once for the first browser, even under StrictMode', async () => {
    const server = serve({ connected: false, claimable: true })
    renderGate()
    expect(await screen.findByText('the app')).toBeInTheDocument()
    expect(server.seen.filter((entry) => entry === 'POST /api/v1/session/claim')).toHaveLength(1)
  })

  it('shows the connect page, never the app, when another browser claimed it', async () => {
    serve({ connected: false, claimable: false })
    renderGate()
    expect(await screen.findByRole('heading', { name: 'Connect this browser' })).toBeInTheDocument()
    expect(screen.getByText('netkeeper open')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Check again' })).toBeInTheDocument()
    expect(screen.queryByText('the app')).not.toBeInTheDocument()
  })

  it('shows the connect page when the claim is refused', async () => {
    serve({ connected: false, claimable: true }, 403)
    renderGate()
    expect(await screen.findByRole('heading', { name: 'Connect this browser' })).toBeInTheDocument()
    expect(screen.queryByText('the app')).not.toBeInTheDocument()
  })

  it('says so when the connect link had expired', async () => {
    window.history.replaceState(null, '', '/?connect=expired')
    serve({ connected: false, claimable: false })
    renderGate()
    expect(await screen.findByRole('alert')).toHaveTextContent('expired or was already used')
  })

  it('goes to the connect page when a call later comes back 401', async () => {
    const server = serve({ connected: true, claimable: false })
    renderGate()
    expect(await screen.findByText('the app')).toBeInTheDocument()
    server.disconnect()
    await api.GET('/api/v1/me')
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Connect this browser' })).toBeInTheDocument()
    })
  })

  it('renders the app when the server cannot be reached', async () => {
    // The default offline fetch throws, as a stopped server would.
    renderGate()
    expect(await screen.findByText('the app')).toBeInTheDocument()
  })
})
