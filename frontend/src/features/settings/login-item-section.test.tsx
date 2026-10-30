import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'

import type { ConfigField, ConfigSettings, LoginItem } from './api'
import { LoginItemSection, MENUBAR_ICON_KEY } from './login-item-section'

const REGISTERED =
  'Registered. netkeep will start by itself the next time you log in to this Mac. Nothing changes now, because netkeep is already running.'
const STARTED =
  'Started. netkeep is starting now, and will start by itself every time you log in to this Mac.'

const OFF: LoginItem = {
  supported: true,
  unsupported_reason: null,
  state: 'Off',
  enabled: false,
  running_under_launchd: false,
  this_server_is_the_login_item: false,
  label: 'fun.tnkr.netkeep',
  plist_path: '/Users/x/Library/LaunchAgents/fun.tnkr.netkeep.plist',
  log_path: '/data/logs/netkeep.log',
  points_elsewhere: null,
  legacy: false,
  launcher: {
    supported: true,
    state: 'Not built',
    exists: false,
    path: '/Users/x/Applications/netkeep.app',
    runs: null,
  },
}

const MENUBAR_HELP =
  'While netkeep runs at login, an icon in the menu bar shows its status. macOS 26 can also hide the icon, but hiding it there leaves the menu-bar app running: turning this off is what stops it.'

const MENUBAR: ConfigField = {
  key: MENUBAR_ICON_KEY,
  group: 'menubar',
  label: 'Show menu-bar icon',
  help: MENUBAR_HELP,
  kind: 'bool',
  applies: 'now',
  applies_note: 'Saved for the menu-bar app.',
  minimum: null,
  maximum: null,
  warn_above: null,
  value: true,
  default: true,
  ui_value: null,
  file_value: null,
  source: 'default',
  editable: true,
  locked_reason: null,
  confirm_text: null,
  restart_pending: false,
  stored_unreadable: false,
  warnings: [],
  notes: [],
}

function config(menubar: ConfigField): ConfigSettings {
  return {
    config_path: null,
    log_file: '/data/logs/netkeep.log',
    config_changed: false,
    config_error: null,
    restart_pending: false,
    can_restart: true,
    busy: [],
    restart_waiting: false,
    fields: [menubar],
  }
}

function renderSection(
  initial: LoginItem,
  onPut?: (enabled: boolean) => Response,
  menubar: ConfigField = MENUBAR,
) {
  const puts: boolean[] = []
  const repairs: boolean[] = []
  const builds: boolean[] = []
  const settingsPuts: unknown[] = []
  let current = config(menubar)
  mockFetch(async (request) => {
    const { pathname } = new URL(request.url)
    if (pathname === '/api/v1/settings/config') {
      if (request.method === 'GET') return jsonResponse(current)
      const body = (await request.json()) as { values: Record<string, unknown> }
      settingsPuts.push(body.values)
      const on = body.values[MENUBAR_ICON_KEY]
      current = config({ ...current.fields[0]!, value: on, ui_value: on, source: 'ui' })
      return jsonResponse(current)
    }
    if (!pathname.startsWith('/api/v1/login-item'))
      return jsonResponse({ detail: 'unexpected' }, 500)
    if (pathname === '/api/v1/login-item/launcher') {
      builds.push(true)
      return jsonResponse({
        ...initial,
        launcher: {
          ...initial.launcher,
          state: 'Built',
          exists: true,
          runs: '/Users/x/.local/bin/netkeep',
        },
        message: 'Built /Users/x/Applications/netkeep.app: it opens netkeep.',
      })
    }
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
  return { puts, repairs, builds, settingsPuts }
}

describe('LoginItemSection', () => {
  it('shows the launcher app and builds it', async () => {
    const { builds, puts } = renderSection(OFF)
    expect(await screen.findByText('Not built')).toBeInTheDocument()
    expect(screen.getByText('/Users/x/Applications/netkeep.app')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Build' }))
    await waitFor(() => expect(builds).toEqual([true]))
    expect(
      await screen.findByText('Built /Users/x/Applications/netkeep.app: it opens netkeep.'),
    ).toBeInTheDocument()
    expect(screen.getByText('/Users/x/.local/bin/netkeep')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Rebuild' })).toBeInTheDocument()
    expect(puts).toEqual([])
  })

  it('hides the launcher app off macOS', async () => {
    renderSection({ ...OFF, launcher: { ...OFF.launcher, supported: false } })
    await screen.findByRole('checkbox', { name: 'Start netkeep when I log in' })
    expect(screen.queryByText('Launcher app')).not.toBeInTheDocument()
  })

  it('shows the state the server read and turns it on', async () => {
    const { puts } = renderSection(OFF)
    const box = await screen.findByRole('checkbox', { name: 'Start netkeep when I log in' })
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
    'Points at a different netkeep',
  ])('shows the state %s', async (state) => {
    renderSection({ ...OFF, state })
    expect(await screen.findByText(state)).toBeInTheDocument()
  })

  it('tells you how to check and links the launchctl entry', async () => {
    renderSection({ ...OFF, enabled: true, state: 'Registered: starts at next login' })
    expect(await screen.findByText(/open netkeep's bookmark without starting/)).toBeInTheDocument()
    expect(screen.getByText('fun.tnkr.netkeep')).toBeInTheDocument()
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
    const box = await screen.findByRole('checkbox', { name: 'Start netkeep when I log in' })
    expect(box).toHaveAttribute('aria-disabled', 'true')
    expect(screen.getByText('Run at login needs macOS.')).toBeInTheDocument()
  })

  it('shows a refusal from the server', async () => {
    renderSection(OFF, () => jsonResponse({ detail: 'launchctl enable failed: nope' }, 409))
    fireEvent.click(await screen.findByRole('checkbox'))
    expect(await screen.findByRole('alert')).toHaveTextContent('launchctl enable failed')
  })

  it('warns when the agent runs another netkeep', async () => {
    renderSection({ ...OFF, enabled: true, points_elsewhere: '/old/checkout/.venv/bin/netkeep' })
    expect(await screen.findByRole('alert')).toHaveTextContent('/old/checkout/.venv/bin/netkeep')
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

  describe('Show menu-bar icon', () => {
    it('is on with no stored value and saves off on its own', async () => {
      const { settingsPuts, puts } = renderSection(OFF)
      const box = await screen.findByRole('checkbox', { name: 'Show menu-bar icon' })
      expect(box).toBeChecked()
      expect(
        screen.getByText(/hiding it there leaves the menu-bar app running/),
      ).toBeInTheDocument()
      fireEvent.click(box)
      await waitFor(() => expect(settingsPuts).toEqual([{ [MENUBAR_ICON_KEY]: false }]))
      await waitFor(() => expect(box).not.toBeChecked())
      expect(puts).toEqual([])
    })

    it('is shown while Run at login is off too', async () => {
      renderSection(OFF)
      expect(
        await screen.findByRole('checkbox', { name: 'Start netkeep when I log in' }),
      ).not.toBeChecked()
      expect(await screen.findByRole('checkbox', { name: 'Show menu-bar icon' })).toBeEnabled()
    })

    it('shows a value config.toml sets locked, with the reason', async () => {
      renderSection(OFF, undefined, {
        ...MENUBAR,
        value: false,
        file_value: false,
        source: 'file',
        editable: false,
        locked_reason: 'Set in /etc/nk.toml, which wins over this page.',
      })
      const box = await screen.findByRole('checkbox', { name: 'Show menu-bar icon' })
      expect(box).not.toBeChecked()
      expect(box).toHaveAttribute('aria-disabled', 'true')
      expect(screen.getByText(/which wins over this page/)).toBeInTheDocument()
    })

    it('shows a refusal from the server', async () => {
      renderSection(OFF)
      const box = await screen.findByRole('checkbox', { name: 'Show menu-bar icon' })
      mockFetch(async () => jsonResponse({ detail: 'menubar.show_icon: nope' }, 422))
      fireEvent.click(box)
      expect(await screen.findByRole('alert')).toHaveTextContent('nope')
    })

    it('is hidden off macOS', async () => {
      const { settingsPuts } = renderSection({
        ...OFF,
        supported: false,
        unsupported_reason: 'Run at login needs macOS.',
        launcher: { ...OFF.launcher, supported: false },
      })
      await screen.findByText('Run at login needs macOS.')
      expect(screen.queryByText('Show menu-bar icon')).not.toBeInTheDocument()
      expect(settingsPuts).toEqual([])
    })
  })
})
