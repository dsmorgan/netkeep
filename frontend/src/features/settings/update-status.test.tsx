import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'

import type { UpdateCheck } from './api'
import { UpdateStatus } from './update-status'

const NONE: UpdateCheck = {
  enabled: true,
  current: '1.2.0',
  checked_at: null,
  ok: null,
  latest: null,
  available: null,
  command: null,
}

function renderStatus(check: UpdateCheck) {
  const requests: string[] = []
  mockFetch((request) => {
    const { pathname } = new URL(request.url)
    requests.push(`${request.method} ${pathname}`)
    if (pathname !== '/api/v1/update-check') return jsonResponse({}, 500)
    return jsonResponse(check)
  })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <UpdateStatus />
    </QueryClientProvider>,
  )
  return { requests }
}

describe('UpdateStatus', () => {
  it('shows a newer release and the command to run, and installs nothing', async () => {
    const { requests } = renderStatus({
      ...NONE,
      checked_at: '2026-10-22T09:00:00Z',
      ok: true,
      latest: '1.3.0',
      available: '1.3.0',
      command: 'netkeep update',
    })
    expect(await screen.findByText('netkeep 1.3.0 is available; this is 1.2.0.')).toBeVisible()
    expect(screen.getByText('netkeep update')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Copy to update, run' })).toBeVisible()
    expect(requests).toEqual(['GET /api/v1/update-check'])
  })

  it('says it is up to date, with when it checked', async () => {
    const at = '2026-10-22T09:00:00Z'
    renderStatus({ ...NONE, checked_at: at, ok: true, latest: '1.2.0' })
    const when = new Date(at).toLocaleString()
    expect(await screen.findByText(`netkeep 1.2.0 is up to date (checked ${when}).`)).toBeVisible()
  })

  it('says a failed check is tried again a day later', async () => {
    renderStatus({ ...NONE, checked_at: '2026-10-22T09:00:00Z', ok: false })
    expect(await screen.findByText(/got no answer; netkeep tries again a day later/)).toBeVisible()
  })

  it('says when no check has run yet', async () => {
    renderStatus(NONE)
    expect(await screen.findByText(/hasn't checked yet/)).toBeVisible()
  })

  it('says the check is off and how to check by hand', async () => {
    renderStatus({ ...NONE, enabled: false })
    expect(await screen.findByText(/The daily check is off, so netkeep asks nothing/)).toBeVisible()
    expect(screen.getByText('netkeep update --check')).toBeVisible()
  })
})
