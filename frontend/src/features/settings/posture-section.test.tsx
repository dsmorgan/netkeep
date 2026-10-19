import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'

import { PostureSection } from './posture-section'
import type { Posture } from './api'

function renderSection(posture: Posture | 'error') {
  mockFetch((request) => {
    const { pathname } = new URL(request.url)
    if (pathname === '/api/v1/posture') {
      if (posture === 'error') return jsonResponse({ detail: 'boom' }, 500)
      return jsonResponse(posture)
    }
    return jsonResponse({ status: 'ok', version: '0.0.1-test' })
  })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <PostureSection />
    </QueryClientProvider>,
  )
}

type Row = Posture['protections'][number]

const CLEAN: Posture = {
  checked_at: '2026-09-24T10:00:00Z',
  timezone: 'America/New_York',
  local_time: '2026-09-24T06:00:00-04:00',
  protections: [
    {
      name: 'browser mode',
      status: 'on',
      value: 'attach only',
      summary: 'attach only',
      actions: [],
      severity: 'on',
      warnings: [],
      notes: [],
    },
    {
      name: 'linkedin session',
      status: 'unknown',
      value: 'not probed',
      summary: 'not probed',
      actions: [],
      severity: 'problem',
      fix: 'Run `netkeep preflight` in a terminal.',
      warnings: ['no browser probe was run, so the session is unknown'],
      notes: [],
    },
  ],
  warnings: ['linkedin session: no browser probe was run, so the session is unknown'],
  notes: [],
  gaps: ['two budgets are not enforced by any running code today'],
  ok: false,
  verdict: 'NOT clear: no protection is disabled, but 1 warning needs reading',
}

describe('PostureSection', () => {
  it('shows every protection with its state and detail, in the >= sm table', async () => {
    renderSection(CLEAN)
    const table = await screen.findByTestId('posture-table')

    const browserRow = within(table).getByText('browser mode').closest('tr') as HTMLElement
    expect(within(browserRow).getByText('on')).toBeInTheDocument()
    expect(within(browserRow).getByText('attach only')).toBeInTheDocument()

    const sessionRow = within(table).getByText('linkedin session').closest('tr') as HTMLElement
    expect(within(sessionRow).getByText('unknown')).toBeInTheDocument()
    expect(
      within(sessionRow).getByText('no browser probe was run, so the session is unknown'),
    ).toBeInTheDocument()
  })

  it('shows every protection with its state and detail, in the below-sm stacked blocks too (M2)', async () => {
    renderSection(CLEAN)
    const blocks = await screen.findByTestId('posture-blocks')

    const browserBlock = within(blocks).getByText('browser mode').closest('li') as HTMLElement
    expect(within(browserBlock).getByText('on')).toBeInTheDocument()
    expect(within(browserBlock).getByText('attach only')).toBeInTheDocument()

    const sessionBlock = within(blocks).getByText('linkedin session').closest('li') as HTMLElement
    expect(within(sessionBlock).getByText('unknown')).toBeInTheDocument()
    expect(
      within(sessionBlock).getByText('no browser probe was run, so the session is unknown'),
    ).toBeInTheDocument()
  })

  it('shows the profile-visit risk as a note under the profile_visits budget, verdict clear (#318)', async () => {
    const risk =
      'Profile visits are set to 150 a day, above the 100 a day netkeep was designed around.' +
      ' More visits a day make it more likely that LinkedIn restricts your account or asks you' +
      ' to verify it. Heat still slows runs down after LinkedIn throttles a visit.'
    renderSection({
      ...CLEAN,
      protections: [
        ...CLEAN.protections,
        {
          name: 'budget profile_visits',
          status: 'on',
          value: '0/150 today, 0/750 this week (hard max 250/day, 1250/week)',
          summary: '0/150 today, 0/750 this week',
          actions: [],
          severity: 'on',
          warnings: [],
          notes: [risk],
        },
      ],
      warnings: [],
      notes: [`budget profile_visits: ${risk}`],
      ok: true,
      verdict: 'nothing is misconfigured: 3 protections, none of them disabled',
    })
    const table = await screen.findByTestId('posture-table')
    fireEvent.click(screen.getByRole('button', { name: 'Show details' }))
    const row = within(table).getByText('budget profile_visits').closest('tr') as HTMLElement
    const notes = within(row).getByRole('list', { name: 'Notes' })
    expect(within(notes).getByText(risk)).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent(/^nothing is misconfigured/)
  })

  it('renders markdown in a gap, not literal asterisks or backticks (review179r2)', async () => {
    renderSection({
      ...CLEAN,
      gaps: [
        '**this report reads configuration and counters, never callers.** Run `netkeep posture` for the terminal version.',
      ],
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Show details' }))
    const bold = await screen.findByText(
      'this report reads configuration and counters, never callers.',
    )
    expect(bold.tagName).toBe('STRONG')
    expect(screen.getByText('netkeep posture').tagName).toBe('CODE')
    expect(screen.queryByText(/\*\*/)).not.toBeInTheDocument()
    expect(screen.queryByText(/`netkeep/)).not.toBeInTheDocument()
  })

  it('shows the gaps list once expanded', async () => {
    renderSection(CLEAN)
    fireEvent.click(await screen.findByRole('button', { name: 'Show details' }))
    expect(screen.getByText('Not covered by this report')).toBeInTheDocument()
    expect(
      screen.getByText('two budgets are not enforced by any running code today'),
    ).toBeInTheDocument()
  })

  it('shows the verdict verbatim, exactly as the backend sends it', async () => {
    renderSection(CLEAN)
    expect(
      await screen.findByText('NOT clear: no protection is disabled, but 1 warning needs reading'),
    ).toBeInTheDocument()
  })

  it('never paraphrases the verdict as "you are safe"', async () => {
    renderSection(CLEAN)
    await screen.findByText(/NOT clear/)
    expect(screen.queryByText(/you are safe/i)).not.toBeInTheDocument()
  })

  it('shows a clean report’s own verdict wording too', async () => {
    renderSection({
      ...CLEAN,
      ok: true,
      warnings: [],
      verdict: 'nothing is misconfigured: 12 protections, none of them disabled',
    })
    expect(
      await screen.findByText('nothing is misconfigured: 12 protections, none of them disabled'),
    ).toBeInTheDocument()
  })

  describe('collapsed vs expanded (#340)', () => {
    const VERBOSE: Posture = {
      ...CLEAN,
      protections: [
        ...CLEAN.protections,
        {
          name: 'budget profile_visits',
          status: 'on',
          value: '0/150 today, 0/750 this week (hard max 250/day, 1250/week)',
          summary: '0/150 today, 0/750 this week',
          actions: [],
          severity: 'on',
          warnings: [],
          notes: ['Profile visits are set to 150 a day, above the 100 a day.'],
        },
      ],
      notes: ['budget profile_visits: Profile visits are set to 150 a day, above the 100 a day.'],
    }

    it('leads with each row’s summary, its warnings, and a note count; no notes or gaps', async () => {
      renderSection(VERBOSE)
      const table = await screen.findByTestId('posture-table')
      const toggle = screen.getByRole('button', { name: 'Show details' })
      expect(toggle).toHaveAttribute('aria-expanded', 'false')
      expect(within(table).getByRole('columnheader', { name: 'Summary' })).toBeInTheDocument()

      const budget = within(table).getByText('budget profile_visits').closest('tr') as HTMLElement
      expect(within(budget).getByText('0/150 today, 0/750 this week')).toBeInTheDocument()
      expect(within(budget).getByText('(1 note)')).toBeInTheDocument()
      expect(screen.queryByText(/hard max/)).not.toBeInTheDocument()
      expect(screen.queryByText(/Profile visits are set to 150/)).not.toBeInTheDocument()
      expect(screen.queryByRole('list', { name: 'Notes' })).not.toBeInTheDocument()
      expect(screen.queryByText('Not covered by this report')).not.toBeInTheDocument()
      expect(screen.getByRole('status')).toHaveTextContent(/^NOT clear/)
    })

    it('always shows a warning, collapsed or expanded, in the table and the stacked blocks', async () => {
      renderSection(VERBOSE)
      const warning = 'no browser probe was run, so the session is unknown'
      const table = await screen.findByTestId('posture-table')
      const blocks = screen.getByTestId('posture-blocks')
      expect(within(table).getByText(warning)).toBeInTheDocument()
      expect(within(blocks).getByText(warning)).toBeInTheDocument()

      fireEvent.click(screen.getByRole('button', { name: 'Show details' }))
      expect(within(table).getByText(warning)).toBeInTheDocument()
      expect(within(blocks).getByText(warning)).toBeInTheDocument()
    })

    it('shows the full value, the notes, and the gaps once expanded, and collapses again', async () => {
      renderSection(VERBOSE)
      const table = await screen.findByTestId('posture-table')
      fireEvent.click(screen.getByRole('button', { name: 'Show details' }))

      const toggle = screen.getByRole('button', { name: 'Hide details' })
      expect(toggle).toHaveAttribute('aria-expanded', 'true')
      expect(within(table).getByRole('columnheader', { name: 'Detail' })).toBeInTheDocument()
      const budget = within(table).getByText('budget profile_visits').closest('tr') as HTMLElement
      expect(
        within(budget).getByText('0/150 today, 0/750 this week (hard max 250/day, 1250/week)'),
      ).toBeInTheDocument()
      expect(within(budget).queryByText('(1 note)')).not.toBeInTheDocument()
      expect(within(budget).getByRole('list', { name: 'Notes' })).toHaveTextContent(
        'Profile visits are set to 150 a day',
      )
      expect(screen.getByText('Not covered by this report')).toBeInTheDocument()

      // The stacked layout below `sm` expands too.
      const blocks = screen.getByTestId('posture-blocks')
      const block = within(blocks).getByText('budget profile_visits').closest('li') as HTMLElement
      expect(
        within(block).getByText('0/150 today, 0/750 this week (hard max 250/day, 1250/week)'),
      ).toBeInTheDocument()
      expect(within(block).getByRole('list', { name: 'Notes' })).toHaveTextContent(
        'Profile visits are set to 150 a day',
      )

      fireEvent.click(toggle)
      expect(screen.getByRole('button', { name: 'Show details' })).toBeInTheDocument()
      expect(screen.queryByRole('list', { name: 'Notes' })).not.toBeInTheDocument()
      expect(screen.queryByText(/hard max/)).not.toBeInTheDocument()
    })
  })

  it('wraps the badge below a stacked name rather than breaking the name mid-word (#181)', async () => {
    // jsdom has no layout, so this pins the classes a real browser at 390px
    // was measured with: the row wraps, and the name may not shrink below its
    // longest word (`min-w-0` is what let "linkedin" split as "linkedi/n").
    renderSection(CLEAN)
    const blocks = await screen.findByTestId('posture-blocks')
    const name = within(blocks).getByText('linkedin session')
    expect(name.parentElement).toHaveClass('flex-wrap')
    expect(name).not.toHaveClass('min-w-0')
  })

  it('shows an error state', async () => {
    renderSection('error')
    expect(await screen.findByRole('alert')).toHaveTextContent('boom')
  })
})

describe('PostureSection: LinkedIn sends and the first inbox poll (#383)', () => {
  const MANUAL_ON: Row = {
    name: 'manual linkedin sends',
    key: 'manual_linkedin_sends',
    status: 'on',
    value: 'auto-send off',
    summary: 'auto-send off',
    actions: [],
    severity: 'on',
    warnings: [],
    notes: [],
  }
  const AUTO_SEND_WARNING =
    'LinkedIn auto-send is on (campaigns.linkedin_auto_send), so netkeep sends' +
    ' LinkedIn messages itself for steps set to Auto-send, rather than prefilling them for you' +
    ' to send. It is off by default: an automated send is the action LinkedIn restricts hardest'
  const SHORT_POLL_WARNING =
    "the first LinkedIn inbox poll couldn't read back to 2030-01-02; check older LinkedIn" +
    ' replies by hand, then click Acknowledge in Settings, Posture, or run' +
    ' `netkeep linkedin inbox-acknowledge`'
  const SHORT_POLL: Row = {
    name: 'linkedin reply poll',
    key: 'linkedin_reply_poll',
    status: 'on',
    value: 'last complete poll 2030-01-03 09:00 UTC (1 h ago)',
    summary: 'last complete poll 2030-01-03 09:00 UTC (1 h ago)',
    actions: [],
    severity: 'warning',
    warnings: [SHORT_POLL_WARNING],
    notes: [],
  }

  function withRows(rows: Posture['protections']): Posture {
    return { ...CLEAN, protections: [...CLEAN.protections, ...rows] }
  }

  it('shows the LinkedIn inbox hold before the first poll, with no Acknowledge button', async () => {
    renderSection(
      withRows([
        {
          ...SHORT_POLL,
          value:
            'no complete poll yet; scheduled polls wait for a first one you run by hand: `netkeep linkedin inbox`',
          summary: 'no complete poll yet',
          actions: [],
          warnings: [],
          notes: [
            'the LinkedIn inbox has not been read: no poll has completed yet; run `netkeep linkedin inbox` by hand; scheduled polls wait for a first one. LinkedIn prefills wait until a poll completes; nothing is skipped or failed',
          ],
        },
      ]),
    )

    fireEvent.click(await screen.findByRole('button', { name: 'Show details' }))
    const notes = (await screen.findAllByRole('list', { name: 'Notes' })).find((list) =>
      list.textContent?.includes('no poll has completed yet'),
    )
    expect(notes).toHaveTextContent('run netkeep linkedin inbox by hand')
    expect(notes).toHaveTextContent('LinkedIn prefills wait until a poll completes')
    expect(screen.queryByRole('button', { name: 'Acknowledge' })).toBeNull()
  })

  it('shows manual LinkedIn sends first and plainly while auto-send is off', async () => {
    renderSection(withRows([MANUAL_ON]))

    const banner = await screen.findByRole('note', { name: 'Manual LinkedIn sends' })
    expect(banner).toHaveAttribute('data-auto-send', 'off')
    expect(banner).toHaveTextContent('Auto-send is off.')
  })

  it('highlights auto-send on as a warning, with the backend’s own text', async () => {
    renderSection(
      withRows([
        {
          ...MANUAL_ON,
          status: 'off',
          value: 'auto-send ON',
          summary: 'auto-send ON',
          actions: [],
          severity: 'warning',
          fix: 'Auto-send is on by your choice; turn off **LinkedIn auto-send** on the Settings page.',
          warnings: [AUTO_SEND_WARNING],
        },
      ]),
    )

    const banner = await screen.findByRole('alert', { name: 'Manual LinkedIn sends' })
    expect(banner).toHaveAttribute('data-auto-send', 'on')
    expect(banner.className).toMatch(/destructive/)
    expect(banner).toHaveTextContent('Auto-send is ON.')
    expect(banner).toHaveTextContent('an automated send is the action LinkedIn restricts hardest')
  })

  it('acknowledges the first inbox poll’s warning with a button, no CLI', async () => {
    let posture = withRows([MANUAL_ON, SHORT_POLL])
    const posted: Request[] = []
    mockFetch((request) => {
      const { pathname } = new URL(request.url)
      if (pathname === '/api/v1/posture') return jsonResponse(posture)
      if (pathname === '/api/v1/linkedin/inbox/acknowledge' && request.method === 'POST') {
        posted.push(request)
        posture = withRows([MANUAL_ON])
        return jsonResponse({ cleared: true })
      }
      return jsonResponse({ detail: 'no fake' }, 404)
    })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <PostureSection />
      </QueryClientProvider>,
    )

    expect(await screen.findByText('LinkedIn replies to check by hand')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Acknowledge' }))

    await waitFor(() => expect(screen.queryByText('LinkedIn replies to check by hand')).toBeNull())
    expect(posted).toHaveLength(1)
    expect(posted[0]?.headers.get('X-Netkeep-Client')).toBe('1')
  })

  it('finds the rows by key, not by name', async () => {
    renderSection(
      withRows([
        { ...MANUAL_ON, name: 'renamed sends row' },
        { ...SHORT_POLL, name: 'renamed poll row' },
      ]),
    )
    expect(await screen.findByRole('note', { name: 'Manual LinkedIn sends' })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Acknowledge' })).toBeVisible()
  })

  function renderAck(rowsAfter: Posture['protections']) {
    let posture = withRows([MANUAL_ON, SHORT_POLL])
    mockFetch((request) => {
      const { pathname } = new URL(request.url)
      if (pathname === '/api/v1/posture') return jsonResponse(posture)
      if (pathname === '/api/v1/linkedin/inbox/acknowledge') {
        posture = withRows(rowsAfter)
        return jsonResponse({ cleared: false })
      }
      return jsonResponse({ detail: 'no fake' }, 404)
    })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <PostureSection />
      </QueryClientProvider>,
    )
  }

  it('says so when there was nothing to acknowledge (cleared elsewhere meanwhile)', async () => {
    renderAck([MANUAL_ON])

    fireEvent.click(await screen.findByRole('button', { name: 'Acknowledge' }))

    expect(await screen.findByText(/Nothing to acknowledge/)).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Acknowledge' })).toBeNull()
  })

  it('keeps the warning and its button while the row still stands', async () => {
    renderAck([MANUAL_ON, SHORT_POLL])

    fireEvent.click(await screen.findByRole('button', { name: 'Acknowledge' }))

    await waitFor(() => expect(screen.getByRole('button', { name: 'Acknowledge' })).toBeEnabled())
    expect(screen.getByText('LinkedIn replies to check by hand')).toBeVisible()
    expect(screen.queryByText(/Nothing to acknowledge/)).toBeNull()
  })

  it('offers no Acknowledge button while no first poll fell short', async () => {
    renderSection(withRows([MANUAL_ON]))
    await screen.findByRole('note', { name: 'Manual LinkedIn sends' })
    expect(screen.queryByRole('button', { name: 'Acknowledge' })).toBeNull()
  })
})

describe('PostureSection: Reset breaker and Forget inbox owner (#583)', () => {
  const ROUTE_WARNING =
    '2 connections runs in a row ended route_changed: scheduled connections runs are skipped' +
    ' until this clears. Run one by hand (`netkeep linkedin sync`) to check whether the wall' +
    ' is still there, or clear it directly: click **Reset breaker** under Posture on the' +
    ' Settings page, or run `netkeep linkedin schedule reset-breaker`'
  const ROUTE_TRIPPED: Row = {
    name: 'route-changed breaker',
    key: 'route_changed_breaker',
    status: 'on',
    value: 'tripped: 2 route_changed connections runs in a row',
    summary: 'tripped: 2 route_changed connections runs in a row',
    actions: ['reset_breaker'],
    severity: 'warning',
    warnings: [ROUTE_WARNING],
    notes: [],
  }
  const ROUTE_CLEAR: Row = {
    ...ROUTE_TRIPPED,
    value: 'clear: no consecutive connections runs have ended route_changed',
    summary: 'clear: no consecutive connections runs have ended route_changed',
    actions: [],
    severity: 'on',
    warnings: [],
  }
  const OWNER_WARNING =
    "2 LinkedIn inbox polls in a row showed another mailbox than this account's. To fix it:" +
    ' if Chrome is signed in to another LinkedIn account, sign back in to yours; otherwise' +
    ' click **Forget inbox owner** under Posture on the Settings page, or run' +
    ' `netkeep linkedin inbox-forget-owner`.'
  const OWNER_TRIPPED: Row = {
    name: 'Inbox owner breaker',
    key: 'inbox_owner_breaker',
    status: 'on',
    value: 'tripped: 2 inbox polls in a row showed another mailbox',
    summary: 'tripped: 2 inbox polls in a row showed another mailbox',
    actions: ['reset_breaker', 'forget_inbox_owner'],
    severity: 'warning',
    warnings: [OWNER_WARNING],
    notes: [],
  }

  function withRows(rows: Posture['protections']): Posture {
    return { ...CLEAN, protections: [...CLEAN.protections, ...rows] }
  }

  function serve(
    initial: Posture,
    routes: Record<string, (request: Request) => Response | Promise<Response>>,
  ) {
    let posture = initial
    const posted: { path: string; body: unknown }[] = []
    mockFetch(async (request) => {
      const { pathname } = new URL(request.url)
      if (pathname === '/api/v1/posture') return jsonResponse(posture)
      const route = routes[pathname]
      if (route !== undefined && request.method === 'POST') {
        posted.push({ path: pathname, body: await request.clone().json() })
        return route(request)
      }
      return jsonResponse({ detail: 'no fake' }, 404)
    })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <PostureSection />
      </QueryClientProvider>,
    )
    return {
      posted,
      set: (next: Posture) => {
        posture = next
      },
    }
  }

  it('offers no button on a clear breaker row', async () => {
    renderSection(withRows([ROUTE_CLEAR]))
    await screen.findByTestId('posture-table')
    expect(screen.queryByRole('button', { name: 'Reset breaker' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Forget inbox owner' })).toBeNull()
  })

  it('shows Reset breaker in the tripped row, in the table and the stacked blocks', async () => {
    renderSection(withRows([ROUTE_TRIPPED]))
    const table = await screen.findByTestId('posture-table')
    const row = within(table).getByText('route-changed breaker').closest('tr') as HTMLElement
    expect(within(row).getByRole('button', { name: 'Reset breaker' })).toBeVisible()
    const blocks = screen.getByTestId('posture-blocks')
    const block = within(blocks).getByText('route-changed breaker').closest('li') as HTMLElement
    expect(within(block).getByRole('button', { name: 'Reset breaker' })).toBeVisible()
    // The browser row has no button.
    const browser = within(table).getByText('browser mode').closest('tr') as HTMLElement
    expect(within(browser).queryByRole('button')).toBeNull()
  })

  it('confirms first, saying what the breaker guards and why it tripped, then resets', async () => {
    const fake = serve(withRows([ROUTE_TRIPPED]), {
      '/api/v1/linkedin/schedule/reset-breaker': () => {
        fake.set(withRows([ROUTE_CLEAR]))
        return jsonResponse({ cleared: true, reset: ['route_changed_breaker'] })
      },
    })
    const table = await screen.findByTestId('posture-table')
    fireEvent.click(within(table).getByRole('button', { name: 'Reset breaker' }))

    const dialog = await screen.findByRole('alertdialog')
    expect(fake.posted).toHaveLength(0)
    expect(dialog).toHaveTextContent('Reset the LinkedIn breaker?')
    expect(dialog).toHaveTextContent(
      'It stops scheduled connections syncs from loading a connections page LinkedIn has changed',
    )
    expect(dialog).toHaveTextContent('Why it tripped:')
    expect(dialog).toHaveTextContent('2 connections runs in a row ended route_changed')
    expect(dialog).toHaveTextContent(
      'scheduled connections, enrichment, and inbox runs may start again when due',
    )

    fireEvent.click(within(dialog).getByRole('button', { name: 'Reset breaker' }))

    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(fake.posted).toEqual([
      {
        path: '/api/v1/linkedin/schedule/reset-breaker',
        body: { confirm: true, tripped: ['route_changed_breaker'] },
      },
    ])
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Reset breaker' })).toBeNull())
  })

  it('Cancel resets nothing', async () => {
    const fake = serve(withRows([ROUTE_TRIPPED]), {
      '/api/v1/linkedin/schedule/reset-breaker': () =>
        jsonResponse({ cleared: true, reset: ['route_changed_breaker'] }),
    })
    const table = await screen.findByTestId('posture-table')
    fireEvent.click(within(table).getByRole('button', { name: 'Reset breaker' }))
    const dialog = await screen.findByRole('alertdialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))

    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(fake.posted).toHaveLength(0)
  })

  it('names every tripped breaker the reset clears, and sends all their keys', async () => {
    const fake = serve(withRows([ROUTE_TRIPPED, OWNER_TRIPPED]), {
      '/api/v1/linkedin/schedule/reset-breaker': () =>
        jsonResponse({ cleared: true, reset: ['inbox_owner_breaker', 'route_changed_breaker'] }),
    })
    const table = await screen.findByTestId('posture-table')
    const owner = within(table).getByText('Inbox owner breaker').closest('tr') as HTMLElement
    fireEvent.click(within(owner).getByRole('button', { name: 'Reset breaker' }))

    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('Reset the LinkedIn breakers?')
    expect(within(dialog).getAllByTestId('reset-breaker-row')).toHaveLength(2)
    expect(dialog).toHaveTextContent('another LinkedIn mailbox than this account’s')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Reset breaker' }))

    await waitFor(() => expect(fake.posted).toHaveLength(1))
    expect(fake.posted[0]?.body).toEqual({
      confirm: true,
      tripped: ['route_changed_breaker', 'inbox_owner_breaker'],
    })
  })

  it('keeps the dialog open with the refusal when the breakers changed meanwhile', async () => {
    serve(withRows([ROUTE_TRIPPED]), {
      '/api/v1/linkedin/schedule/reset-breaker': () =>
        jsonResponse(
          {
            detail:
              'the tripped breakers are now inbox_owner_breaker, route_changed_breaker, not what you were shown; reload Posture and confirm again',
          },
          409,
        ),
    })
    const table = await screen.findByTestId('posture-table')
    fireEvent.click(within(table).getByRole('button', { name: 'Reset breaker' }))
    const dialog = await screen.findByRole('alertdialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Reset breaker' }))

    expect(await within(dialog).findByRole('alert')).toHaveTextContent('reload Posture')
    expect(screen.getByRole('alertdialog')).toBeVisible()
  })

  it('forgets the inbox owner after a confirmation that says when not to', async () => {
    const fake = serve(withRows([OWNER_TRIPPED]), {
      '/api/v1/linkedin/inbox/forget-owner': () => {
        fake.set(withRows([{ ...OWNER_TRIPPED, actions: ['reset_breaker'] }]))
        return jsonResponse({ forgotten: true })
      },
    })
    const table = await screen.findByTestId('posture-table')
    fireEvent.click(within(table).getByRole('button', { name: 'Forget inbox owner' }))

    const dialog = await screen.findByRole('alertdialog')
    expect(fake.posted).toHaveLength(0)
    expect(dialog).toHaveTextContent('Forget the recorded LinkedIn mailbox?')
    expect(dialog).toHaveTextContent('cancel and sign back in to yours instead')
    expect(dialog).toHaveTextContent('the next inbox poll records whichever account the page shows')
    expect(dialog).toHaveTextContent(
      'click Check inbox now on the LinkedIn page, or run netkeep linkedin inbox.',
    )
    fireEvent.click(within(dialog).getByRole('button', { name: 'Forget inbox owner' }))

    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(fake.posted).toEqual([
      { path: '/api/v1/linkedin/inbox/forget-owner', body: { confirm: true } },
    ])
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Forget inbox owner' })).toBeNull(),
    )
    expect(within(table).getByRole('button', { name: 'Reset breaker' })).toBeVisible()
  })

  it('shows Forget inbox owner on whichever row the backend offers it, such as the reply poll', async () => {
    renderSection(
      withRows([
        {
          name: 'linkedin reply poll',
          key: 'linkedin_reply_poll',
          status: 'on',
          value: 'no complete poll yet',
          summary: 'no complete poll yet',
          actions: ['forget_inbox_owner'],
          severity: 'on',
          warnings: [],
          notes: ['the newest poll ended owner_mismatch'],
        },
      ]),
    )
    const table = await screen.findByTestId('posture-table')
    const row = within(table).getByText('linkedin reply poll').closest('tr') as HTMLElement
    expect(within(row).getByRole('button', { name: 'Forget inbox owner' })).toBeVisible()
    expect(within(row).queryByRole('button', { name: 'Reset breaker' })).toBeNull()
  })
})
