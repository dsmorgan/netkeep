import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { navigation } from '@/features/mailboxes/api'
import { mailbox } from '@/features/mailboxes/test-support'

import { FakeEventSource, resetFakeEventSource } from './fake-event-source'
import { jsonResponse } from './fetch'
import { attention, linkedinStatus, quiet, serve } from './install'
import { renderApp } from './render'

/** The tab key the two-tab dashboard (#667) kept; the one page ignores it (#739). */
const OLD_TAB_KEY = 'netkeep.dashboard.tab'

beforeEach(() => {
  vi.stubGlobal('EventSource', FakeEventSource)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  resetFakeEventSource()
  window.localStorage.clear()
})

describe('dashboard: one page (#739)', () => {
  it('lays out the header, metrics slot, setup checklist and details, in that order', async () => {
    serve()
    await renderApp('/')
    await screen.findByTestId('setup-progress')

    const main = screen.getByRole('main')
    const regions = [...main.querySelectorAll('[data-region]')].map((node) =>
      node.getAttribute('data-region'),
    )
    expect(regions).toEqual([
      'header',
      'metrics',
      'setup',
      'needs-you',
      'running',
      'coming-up',
      'campaigns',
      'changed-jobs',
    ])
    // The status chips moved to the top bar (#751): the header keeps only Needs you.
    const header = main.querySelector('[data-region="header"]') as HTMLElement
    expect(within(header).getByTestId('needs-you-line')).toHaveTextContent(
      'Nothing needs you right now.',
    )
    expect(within(main).queryByRole('group', { name: 'System status' })).not.toBeInTheDocument()
    expect(within(header).queryByTestId('needs-you-chip')).not.toBeInTheDocument()
    expect(within(main).queryByText(/Live updates/)).not.toBeInTheDocument()
  })

  it('has no tabs, and ignores the tab the old dashboard remembered', async () => {
    window.localStorage.setItem(OLD_TAB_KEY, 'overview')
    serve()
    await renderApp('/')

    // Both of what were two tabs show at once: the setup checklist and the detail.
    expect(await screen.findByTestId('setup-progress')).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Needs you' })).toBeInTheDocument()
    expect(screen.queryByRole('tablist')).not.toBeInTheDocument()
    expect(screen.queryByRole('tab')).not.toBeInTheDocument()
    // Nothing writes the key back.
    expect(window.localStorage.getItem(OLD_TAB_KEY)).toBe('overview')
  })

  it('fills the metrics region with the tiles, under its window heading', async () => {
    serve()
    await renderApp('/')
    const region = screen
      .getByRole('main')
      .querySelector('[data-region="metrics"]') as HTMLElement | null
    expect(region).not.toBeNull()
    expect(region).toHaveAccessibleName('Last 7 days')
    const tiles = await within(region as HTMLElement).findByRole('list', { name: 'Metrics' })
    expect(within(tiles).getAllByRole('listitem')).toHaveLength(5)
  })

  it('dates the header and says how many things need you', async () => {
    serve({
      '/api/v1/attention': attention({
        counts: { inbox: 2, dashboard: 1, waiting: 1, try_again: 0, triage: 9, imports: 0 },
      }),
      '/api/v1/linkedin/status': linkedinStatus({ heat_tripped: true }),
      '/api/v1/linkedin/heat': { ...(quiet()['/api/v1/linkedin/heat'] as object), tripped: true },
    })
    await renderApp('/')
    const header = screen.getByRole('main').querySelector('[data-region="header"]') as HTMLElement
    // A problem, a LinkedIn step waiting, and two unhandled Inbox items.
    await waitFor(() =>
      expect(within(header).getByTestId('needs-you-line')).toHaveTextContent('4 things need you.'),
    )
    const weekday = new Date().toLocaleDateString(undefined, { weekday: 'long' })
    expect(within(header).getByText(new RegExp(`^${weekday}, .+ · .+`))).toBeInTheDocument()
  })

  it('links its one chip to Needs you, red while something stopped', async () => {
    serve({
      '/api/v1/attention': attention({
        counts: { inbox: 2, dashboard: 1, waiting: 1, try_again: 0, triage: 9, imports: 0 },
      }),
    })
    await renderApp('/')
    const header = screen.getByRole('main').querySelector('[data-region="header"]') as HTMLElement
    const chip = await within(header).findByRole('link', { name: 'Needs you · 3' })
    expect(chip).toHaveAttribute('href', '/#needs-you')
    expect(chip).toHaveAttribute('data-tone', 'busy')
  })

  it('tints the Needs you chip red for a problem', async () => {
    serve({ '/api/v1/linkedin/status': linkedinStatus({ session_flag: 'checkpoint' }) })
    await renderApp('/')
    const chip = await screen.findByRole('link', { name: 'Needs you · 1' })
    expect(chip).toHaveAttribute('data-tone', 'bad')
    expect(chip.querySelector('svg')).not.toBeNull()
  })
})

describe('dashboard: Needs you', () => {
  function needsYou() {
    return within(screen.getByRole('region', { name: 'Needs you' }))
  }

  it('holds the problems first, then the replies and the LinkedIn steps', async () => {
    serve({
      '/api/v1/linkedin/status': linkedinStatus({ session_flag: 'logged_out' }),
      '/api/v1/attention': attention({
        counts: { inbox: 2, dashboard: 0, waiting: 0, try_again: 0, triage: 0, imports: 0 },
      }),
    })
    await renderApp('/')
    const problems = await needsYou().findByRole('list', { name: 'Problems' })
    const problem = within(problems).getByRole('listitem')
    expect(problem).toHaveTextContent('Logged out of LinkedIn')
    expect(within(problem).getByRole('link', { name: 'See the fix' })).toHaveAttribute(
      'href',
      '/linkedin#session',
    )
    expect(needsYou().getByRole('link', { name: 'Open the Inbox' })).toHaveAttribute(
      'href',
      '/inbox',
    )

    const headings = needsYou()
      .getAllByRole('heading')
      .map((heading) => [heading.tagName, heading.textContent])
    expect(headings).toEqual([
      ['H2', 'Needs you'],
      ['H3', 'Logged out of LinkedIn'],
      ['H3', 'Replies this week'],
      ['H3', 'LinkedIn queue'],
      ['H3', 'Waiting for you'],
    ])
    // The pane is tinted, as everything that needs you is.
    expect(screen.getByRole('region', { name: 'Needs you' })).toHaveClass('nk-tint')
  })

  it('re-authorizes a signed-out mailbox on Google’s page', async () => {
    const assign = vi.spyOn(navigation, 'assign').mockImplementation(() => {})
    const calls = serve({
      '/api/v1/mailboxes': [mailbox({ status: 'reauth_required' })],
      'POST /api/v1/mailboxes/oauth/start': {
        authorization_url: 'https://accounts.example/auth?x=1',
      },
    })
    await renderApp('/')
    fireEvent.click(await needsYou().findByRole('button', { name: 'Re-authorize' }))
    await waitFor(() => expect(assign).toHaveBeenCalledWith('https://accounts.example/auth?x=1'))
    const start = calls.find((call) => call.path === '/api/v1/mailboxes/oauth/start')
    expect(JSON.parse(start?.body ?? '{}')).toEqual({ mailbox_id: 3 })
    expect(needsYou().getByRole('link', { name: 'Settings' })).toHaveAttribute(
      'href',
      '/settings#gmail',
    )
  })

  it('says so when re-authorizing fails', async () => {
    serve({
      '/api/v1/mailboxes': [mailbox({ status: 'reauth_required' })],
      'POST /api/v1/mailboxes/oauth/start': () => jsonResponse({ detail: 'Google refused' }, 500),
    })
    await renderApp('/')
    fireEvent.click(await needsYou().findByRole('button', { name: 'Re-authorize' }))
    expect(await needsYou().findByText('Google refused')).toBeInTheDocument()
  })

  it('starts nothing on its own: every request it makes is a read', async () => {
    const calls = serve()
    await renderApp('/')
    await screen.findByTestId('setup-progress')
    await waitFor(() =>
      expect(document.querySelector('[data-system="linkedin"]')).toHaveAttribute('data-tone', 'ok'),
    )
    expect(calls.filter((call) => call.method !== 'GET')).toEqual([])
  })

  it('reads heat only while the LinkedIn status says it tripped (#752)', async () => {
    const calls = serve()
    await renderApp('/')
    await waitFor(() =>
      expect(calls.some((call) => call.path === '/api/v1/linkedin/status')).toBe(true),
    )
    await screen.findByTestId('needs-you-line')
    expect(calls.filter((call) => call.path === '/api/v1/linkedin/heat')).toEqual([])
  })
})
