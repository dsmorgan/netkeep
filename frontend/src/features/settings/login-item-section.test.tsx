import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'

import type { LoginItem } from './api'
import { LoginItemSection } from './login-item-section'

const REGISTERED =
  'Registered. netkeeper will start by itself the next time you log in to this Mac. Nothing changes now, because netkeeper is already running.'
const STARTED =
  'Started. netkeeper is starting now, and will start by itself every time you log in to this Mac.'

const OFF: LoginItem = {
  supported: true,
  unsupported_reason: null,
  state: 'Off',
  enabled: false,
  running_under_launchd: false,
  this_server_is_the_login_item: false,
  label: 'fun.tnkr.netkeeper',
  plist_path: '/Users/x/Library/LaunchAgents/fun.tnkr.netkeeper.plist',
  log_path: '/data/logs/netkeeper.log',
  points_elsewhere: null,
  legacy: false,
}

function renderSection(initial: LoginItem, onPut?: (enabled: boolean) => Response) {
  const puts: boolean[] = []
  const repairs: boolean[] = []
  mockFetch(async (request) => {
    const { pathname } = new URL(request.url)
    if (!pathname.startsWith('/api/v1/login-item'))
      return jsonResponse({ detail: 'unexpected' }, 500)
    if (pathname === '/api/v1/login-item/repair') {
      repairs.push(true)
      return jsonResponse({
        ...initial,
        legacy: false,
        points_elsewhere: null,
        message: 'Repaired.',
      })
    }
    if (request.method === 'GET') return jsonResponse(initial)
    const body = (await request.json()) as { enabled: boolean }
    puts.push(body.enabled)
    if (onPut) return onPut(body.enabled)
    return jsonResponse({
      ...initial,
      enabled: body.enabled,
      state: 'Registered: starts at next login',
      message: REGISTERED,
    })
  })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <LoginItemSection />
    </QueryClientProvider>,
  )
  return { puts, repairs }
}

describe('LoginItemSection', () => {
  it('shows the state the server read and turns it on', async () => {
    const { puts } = renderSection(OFF)
    const box = await screen.findByRole('checkbox', { name: 'Start netkeeper when I log in' })
    expect(box).not.toBeChecked()
    fireEvent.click(box)
    await waitFor(() => expect(puts).toEqual([true]))
    expect(await screen.findByText(REGISTERED)).toBeInTheDocument()
    expect(screen.getByText('Registered: starts at next login')).toBeInTheDocument()
    await waitFor(() => expect(box).toBeChecked())
  })

  it('says what happened when the CLI-style start ran', async () => {
    renderSection(OFF, () =>
      jsonResponse({
        ...OFF,
        enabled: true,
        running_under_launchd: true,
        state: 'Running from login agent',
        message: STARTED,
      }),
    )
    fireEvent.click(await screen.findByRole('checkbox'))
    expect(await screen.findByText(STARTED)).toBeInTheDocument()
    expect(screen.queryByText(/Nothing changes now/)).not.toBeInTheDocument()
  })

  it.each([
    'Off',
    'Registered: starts at next login',
    'Running from login agent',
    'Needs repair',
    'Points at a different netkeeper',
  ])('shows the state %s', async (state) => {
    renderSection({ ...OFF, state })
    expect(await screen.findByText(state)).toBeInTheDocument()
  })

  it('tells you how to check and links the launchctl entry', async () => {
    renderSection({ ...OFF, enabled: true, state: 'Registered: starts at next login' })
    expect(
      await screen.findByText(/open netkeeper's bookmark without starting/),
    ).toBeInTheDocument()
    expect(screen.getByText('fun.tnkr.netkeeper')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: "If the page doesn't open" })).toHaveAttribute(
      'href',
      expect.stringContaining('troubleshooting.md#run-at-login-does-nothing'),
    )
  })

  it('hides the How to check line while it is off', async () => {
    renderSection(OFF)
    await screen.findByText('Off')
    expect(screen.queryByText(/How to check/)).not.toBeInTheDocument()
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

  it('offers Repair for an old script agent, and never says to turn it off and on', async () => {
    const { puts, repairs } = renderSection({ ...OFF, legacy: true })
    expect(await screen.findByText(/old install script/)).toBeInTheDocument()
    expect(screen.queryByText(/off and on/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Repair' }))
    expect(await screen.findByText('Repaired.')).toBeInTheDocument()
    expect(repairs).toEqual([true])
    expect(puts).toEqual([])
  })
})
