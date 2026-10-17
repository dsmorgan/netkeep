import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'

import type { LoginItem } from './api'
import { LoginItemSection } from './login-item-section'

const OFF: LoginItem = {
  supported: true,
  unsupported_reason: null,
  enabled: false,
  running_under_launchd: false,
  this_server_is_the_login_item: false,
  label: 'fun.tnkr.netkeeper',
  plist_path: '/Users/x/Library/LaunchAgents/fun.tnkr.netkeeper.plist',
  log_path: '/data/logs/netkeeper.log',
  points_elsewhere: null,
}

function renderSection(initial: LoginItem, onPut?: (enabled: boolean) => Response) {
  const puts: boolean[] = []
  mockFetch(async (request) => {
    const { pathname } = new URL(request.url)
    if (pathname !== '/api/v1/login-item') return jsonResponse({ detail: 'unexpected' }, 500)
    if (request.method === 'GET') return jsonResponse(initial)
    const body = (await request.json()) as { enabled: boolean }
    puts.push(body.enabled)
    if (onPut) return onPut(body.enabled)
    return jsonResponse({
      ...initial,
      enabled: body.enabled,
      message: 'Turned on. netkeeper is running now, so launchd will start it at your next login.',
    })
  })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <LoginItemSection />
    </QueryClientProvider>,
  )
  return puts
}

describe('LoginItemSection', () => {
  it('shows the state the server read and turns it on', async () => {
    const puts = renderSection(OFF)
    const box = await screen.findByRole('checkbox', { name: 'Start netkeeper when I log in' })
    expect(box).not.toBeChecked()
    fireEvent.click(box)
    await waitFor(() => expect(puts).toEqual([true]))
    expect(await screen.findByText(/start it at your next login/)).toBeInTheDocument()
    await waitFor(() => expect(box).toBeChecked())
  })

  it('says why the switch is disabled off macOS', async () => {
    renderSection({ ...OFF, supported: false, unsupported_reason: 'Run at login needs macOS.' })
    const box = await screen.findByRole('checkbox', { name: 'Start netkeeper when I log in' })
    expect(box).toHaveAttribute('aria-disabled', 'true')
    expect(screen.getByText('Run at login needs macOS.')).toBeInTheDocument()
  })

  it('shows a refusal from the server', async () => {
    renderSection(OFF, () => jsonResponse({ detail: 'launchctl enable failed: nope' }, 409))
    fireEvent.click(await screen.findByRole('checkbox'))
    expect(await screen.findByRole('alert')).toHaveTextContent('launchctl enable failed')
  })

  it('warns when the agent runs another netkeeper', async () => {
    renderSection({ ...OFF, enabled: true, points_elsewhere: '/old/checkout/.venv/bin/netkeeper' })
    expect(await screen.findByRole('alert')).toHaveTextContent('/old/checkout/.venv/bin/netkeeper')
  })
})
