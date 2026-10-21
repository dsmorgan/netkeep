import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { GRAPH } from '@/features/dashboard/steps-fixtures'
import { navigation } from '@/features/mailboxes/api'
import { mailbox } from '@/features/mailboxes/test-support'

import { FakeEventSource, resetFakeEventSource } from './fake-event-source'
import { jsonResponse, mockFetch } from './fetch'
import { renderApp } from './render'

/** The tab key the two-tab dashboard (#667) kept; the one page ignores it (#739). */
const OLD_TAB_KEY = 'netkeep.dashboard.tab'

function attention(overrides: Record<string, unknown> = {}) {
  return {
    checked_at: '2026-10-20T12:00:00Z',
    counts: { inbox: 0, dashboard: 0, waiting: 0, try_again: 0, triage: 0, imports: 0 },
    flags: { dashboard: null, gmail: null, linkedin: null, settings: null },
    activity: { state: 'idle', label: '', run_id: null, run_kind: null },
    title_total: 0,
    ...overrides,
  }
}

function linkedinStatus(overrides: Record<string, unknown> = {}) {
  return {
    session_flag: null,
    session_flagged_at: null,
    heat_tripped: false,
    armed: true,
    schedule_paused: false,
    running_run_id: null,
    can_start_runs: true,
    ...overrides,
  }
}

/** Every read the dashboard makes, answered with a quiet, healthy install. */
function quiet(): Record<string, unknown> {
  return {
    '/api/v1/health': { status: 'ok', version: '0.0.1-test' },
    '/api/v1/setup': {
      complete: false,
      steps: GRAPH.map((step) => ({ ...step, title: `Step ${step.key}`, reason: 'Waits.' })),
    },
    '/api/v1/attention': attention(),
    '/api/v1/mailboxes': [mailbox({ arm: 'send', armed_at: '2026-09-21T12:00:00Z' })],
    '/api/v1/linkedin/status': linkedinStatus(),
    '/api/v1/linkedin/runs': { items: [], total: 0 },
    '/api/v1/linkedin/schedule': {
      armed: true,
      armed_at: '2026-09-20T12:00:00Z',
      paused: false,
      paused_at: null,
      scheduler_running: true,
      jobs: [],
    },
    '/api/v1/linkedin/budget': {
      budgets: [{ action: 'li_prefills', day: { count: 13, limit: 15, remaining: 2 }, week: null }],
      profile_visits_today: {
        ramp: 10,
        after_weekend: 10,
        after_heat: 10,
        spent_today: 0,
        week_left: null,
        remaining: 10,
      },
      profile_view_notice: 'Enrichment can show up in Who viewed your profile.',
      risk_warning: null,
    },
    '/api/v1/linkedin/heat': {
      score: 0.5,
      threshold: 5,
      multiplier: 1,
      tripped: false,
      last_raised_at: null,
      cleared_at: null,
      resumes_at: null,
    },
    '/api/v1/dashboard/metrics': {
      window: '7d',
      start: '2026-10-14',
      end: '2026-10-20',
      sent: 0,
      replies: 0,
      reply_rate: null,
      bounced: 0,
      changed_jobs: 0,
      new_contacts: { total: 0, linkedin: 0, imported: 0 },
      series: [],
    },
    '/api/v1/dashboard/next-fires': { items: [], total: 0 },
    '/api/v1/dashboard/changed-jobs': { items: [], total: 0, days: 30 },
    '/api/v1/dashboard/inbound': {
      count: 0,
      since: '2026-10-13T12:00:00Z',
      reply_detection: false,
    },
    '/api/v1/campaigns': [],
    '/api/v1/campaigns/linkedin/ready': { items: [], total: 0, try_again: [] },
    '/api/v1/campaigns/linkedin/waiting': { items: [], total: 0 },
    '/api/v1/campaigns/linkedin/options': { auto_send: false },
  }
}

type Answer = unknown | ((request: Request) => Response | Promise<Response>)

/** Serves `quiet()` with `overrides` on top; anything else answers 404. */
function serve(overrides: Record<string, Answer> = {}) {
  const calls: { method: string; path: string; body: string }[] = []
  const routes: Record<string, Answer> = { ...quiet(), ...overrides }
  mockFetch(async (request) => {
    const { pathname } = new URL(request.url)
    const body = request.method === 'GET' ? '' : await request.clone().text()
    calls.push({ method: request.method, path: pathname, body })
    const answer = routes[`${request.method} ${pathname}`] ?? routes[pathname]
    if (answer === undefined) return new Response('not found', { status: 404 })
    return typeof answer === 'function'
      ? (answer as (request: Request) => Response | Promise<Response>)(request)
      : jsonResponse(answer)
  })
  return calls
}

function chips() {
  return within(screen.getByRole('list', { name: 'Status' }))
}

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
  it('lays out the header, chips, metrics slot, setup checklist and details, in that order', async () => {
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
    // The chips sit in the header, under the line about what needs you.
    const header = main.querySelector('[data-region="header"]') as HTMLElement
    expect(within(header).getByRole('list', { name: 'Status' })).toBeInTheDocument()
    expect(within(header).getByTestId('needs-you-line')).toHaveTextContent(
      'Nothing needs you right now.',
    )
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

  it('keeps the live updates indicator, which reads disconnected before the stream opens', async () => {
    serve()
    await renderApp('/')
    expect(
      await within(screen.getByRole('main')).findByText('Live updates disconnected'),
    ).toBeInTheDocument()
  })
})

describe('dashboard: the status chips', () => {
  it('links each chip to the page or section that fixes it, in order', async () => {
    serve({
      '/api/v1/linkedin/status': linkedinStatus({ running_run_id: 9 }),
      '/api/v1/linkedin/runs': {
        items: [{ id: 9, kind: 'enrich', status: 'running', trigger: 'manual' }],
        total: 1,
      },
      '/api/v1/linkedin/heat': { ...(quiet()['/api/v1/linkedin/heat'] as object), tripped: true },
    })
    await renderApp('/')
    await chips().findByRole('link', { name: 'Enrichment running' })

    expect(
      chips()
        .getAllByRole('link')
        .map((link) => [link.textContent, link.getAttribute('href')]),
    ).toEqual([
      ['Gmail sending', '/gmail'],
      ['No LinkedIn session flag', '/linkedin#session'],
      ['Enrichment running', '/linkedin#runs'],
      ['Scheduled runs armed', '/linkedin#schedule'],
      ['2 of 15 prefills left', '/settings#linkedin-budget'],
      ['1 problem', '/#needs-you'],
    ])
  })

  it('shows no running chip and no problems chip while neither applies', async () => {
    serve()
    await renderApp('/')
    await chips().findByRole('link', { name: 'Scheduled runs armed' })
    expect(chips().getAllByRole('link')).toHaveLength(4)
    expect(chips().queryByRole('link', { name: /running|problem/ })).not.toBeInTheDocument()
  })

  it('tints a chip with its state', async () => {
    serve({ '/api/v1/linkedin/status': linkedinStatus({ session_flag: 'checkpoint' }) })
    await renderApp('/')
    const session = await chips().findByRole('link', { name: 'LinkedIn checkpoint' })
    expect(session.closest('li')).toHaveAttribute('data-tone', 'bad')
    expect(chips().getByRole('link', { name: 'Gmail sending' }).closest('li')).toHaveAttribute(
      'data-tone',
      'ok',
    )
  })

  it('says it could not check, chip by chip, when the backend is unreachable', async () => {
    // The default handler rejects every fetch (src/test/fetch.ts).
    await renderApp('/')
    expect(await chips().findByRole('link', { name: 'Gmail: couldn’t check' })).toHaveAttribute(
      'href',
      '/settings#gmail',
    )
    expect(chips().getByRole('link', { name: 'LinkedIn: couldn’t check' })).toBeInTheDocument()
    // The rest of the page still renders, each region with its own failure.
    expect(await screen.findByText('Setup couldn’t be read')).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Needs you' })).toBeInTheDocument()
  })

  it('reads the mailbox list while the Keychain is locked, never the mailbox status', async () => {
    const calls = serve({
      '/api/v1/mailboxes/status': () => jsonResponse({ detail: 'the Keychain is locked' }, 503),
    })
    await renderApp('/')
    await chips().findByRole('link', { name: 'Gmail sending' })
    expect(calls.some((call) => call.path === '/api/v1/mailboxes/status')).toBe(false)
  })

  it('changes the Gmail chip on the mailbox.status server event, without a reload', async () => {
    let rows = [mailbox({ arm: 'send', armed_at: '2026-09-21T12:00:00Z' })]
    serve({ '/api/v1/mailboxes': () => jsonResponse(rows) })
    await renderApp('/')
    await chips().findByRole('link', { name: 'Gmail sending' })

    rows = [mailbox({ status: 'reauth_required', status_reason: 'invalid_grant' })]
    act(() => {
      FakeEventSource.instances.at(-1)?.emit('mailbox.status', {
        mailbox_id: 3,
        status: 'reauth_required',
        reason: 'invalid_grant',
      })
    })
    expect(
      await chips().findByRole('link', { name: 'Gmail needs re-authorizing' }),
    ).toBeInTheDocument()
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
    await chips().findByRole('link', { name: 'Scheduled runs armed' })
    expect(calls.filter((call) => call.method !== 'GET')).toEqual([])
  })
})
