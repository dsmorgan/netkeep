import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { linkedinKeys } from '@/features/linkedin/api'
import {
  BUDGET,
  RISK_WARNING,
  backend,
  type Call,
  type Handler,
} from '@/features/linkedin/test-support'
import { pollStatusKeys } from '@/features/poll-status/api'
import { systemStatusKeys } from '@/features/system-status/api'
import { jsonResponse, mockFetch } from '@/test/fetch'

import type { Automation, GmailMove, LinkedInMove } from './api'
import { AutomationCard } from './automation-card'
import { automation, BLOCKED_REASON } from './test-support'

/**
 * Settings › Automation against a fake backend that keeps the switch's state: each
 * `PUT` answers the state it moved to, unless the test's handler says otherwise.
 */
function renderCard(start: Automation = automation(), handlers: Record<string, Handler> = {}) {
  let state = start
  const calls: Call[] = []
  mockFetch(
    backend(
      {
        'GET /api/v1/automation': () => jsonResponse(state),
        'GET /api/v1/linkedin/budget': () => jsonResponse(BUDGET),
        'GET /api/v1/poll-status': () =>
          jsonResponse({
            background_running: true,
            checked_at: '2026-11-15T12:00:00Z',
            items: [
              {
                key: 'gmail_replies',
                label: 'Gmail replies',
                group: 'gmail',
                interval_minutes: 10,
                last_at: null,
                next_at: null,
                reason: null,
                requested: false,
                requested_at: null,
                state: 'manual',
              },
            ],
            mailboxes: [],
          }),
        'PUT /api/v1/automation/linkedin': (call) => {
          const body = call.body as LinkedInMove
          state = {
            ...state,
            linkedin: {
              ...state.linkedin,
              position: body.position,
              depth: body.depth ?? state.linkedin.depth,
            },
          }
          return jsonResponse(state)
        },
        'PUT /api/v1/automation/gmail': (call) => {
          const body = call.body as GmailMove
          state = { ...state, gmail: { ...state.gmail, position: body.position } }
          return jsonResponse(state)
        },
        ...handlers,
      },
      calls,
    ),
  )
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <AutomationCard />
    </QueryClientProvider>,
  )
  const puts = (service: 'linkedin' | 'gmail') =>
    calls.filter((call) => call.method === 'PUT' && call.path === `/api/v1/automation/${service}`)
  return { calls, puts, queryClient }
}

async function group(name: 'LinkedIn automation' | 'Gmail automation') {
  return within(await screen.findByRole('radiogroup', { name }))
}

/** The checked stop's text; `hidden`, since a modal dialog hides the page while open. */
function checked(radios: ReturnType<typeof within>): string | null {
  const on = radios
    .getAllByRole('radio', { hidden: true })
    .find((radio: HTMLElement) => radio.getAttribute('aria-checked') === 'true')
  return on?.textContent ?? null
}

describe('Settings › Automation (#829)', () => {
  it('shows each service’s three positions with a line each, the server’s one checked', async () => {
    renderCard(automation({ gmail: { position: 'off' } }))
    const linkedin = await group('LinkedIn automation')
    expect(linkedin.getAllByRole('radio').map((radio) => radio.textContent)).toEqual([
      expect.stringContaining('Off'),
      expect.stringContaining('Manual'),
      expect.stringContaining('Automatic'),
    ])
    expect(linkedin.getByRole('radio', { name: 'Manual' })).toHaveAccessibleDescription(
      'Runs when you click Start',
    )
    const gmail = await group('Gmail automation')
    expect(gmail.getByRole('radio', { name: 'Off' })).toHaveAttribute('aria-checked', 'true')
    expect(gmail.getByRole('radio', { name: 'Manual' })).toHaveAccessibleDescription(
      'Writes drafts; you send them',
    )
    expect(await gmail.findByText(/checks replies every 10 min/)).toBeInTheDocument()
    // How thorough shows only on Automatic.
    expect(screen.queryByRole('radiogroup', { name: 'How thorough' })).toBeNull()
  })

  it('moves left in one click, with no dialog and no confirm', async () => {
    const { puts } = renderCard()
    const linkedin = await group('LinkedIn automation')

    fireEvent.click(linkedin.getByRole('radio', { name: 'Off' }))

    await waitFor(() =>
      expect(linkedin.getByRole('radio', { name: 'Off' })).toHaveAttribute('aria-checked', 'true'),
    )
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(puts('linkedin').map((call) => call.body)).toEqual([{ position: 'off', confirm: false }])
  })

  it('moves Automatic back to Manual without asking', async () => {
    const { puts } = renderCard(automation({ linkedin: { position: 'automatic' } }))
    const linkedin = await group('LinkedIn automation')
    fireEvent.click(linkedin.getByRole('radio', { name: 'Manual' }))
    await waitFor(() => expect(checked(linkedin)).toContain('Manual'))
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(puts('linkedin')[0]?.body).toEqual({ position: 'manual', confirm: false })
  })

  it('asks before Automatic, lists what starts on its own, and sends confirm: true', async () => {
    const { puts } = renderCard(undefined, {
      'GET /api/v1/linkedin/budget': () => jsonResponse({ ...BUDGET, risk_warning: RISK_WARNING }),
    })
    const linkedin = await group('LinkedIn automation')

    fireEvent.click(linkedin.getByRole('radio', { name: 'Automatic' }))

    const dialog = await screen.findByRole('alertdialog', {
      name: 'Let netkeep use LinkedIn on its own?',
    })
    expect(dialog).toHaveTextContent('from Manual to Automatic: full upkeep')
    const list = within(within(dialog).getByRole('list', { name: 'Starts on its own' }))
    expect(list.getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      'checks for new connections',
      'LinkedIn inbox checks',
      'a weekly full sync',
      'profile refreshes',
    ])
    expect(
      await within(dialog).findByRole('note', { name: 'Profile-visit risk' }),
    ).toHaveTextContent(RISK_WARNING)
    expect(within(dialog).getByRole('note', { name: 'Profile views' })).toBeInTheDocument()
    // Nothing was sent, and the track still shows the server's position.
    expect(puts('linkedin')).toEqual([])
    expect(checked(linkedin)).toContain('Manual')

    fireEvent.click(within(dialog).getByRole('button', { name: 'Turn on Automatic' }))

    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(puts('linkedin').map((call) => call.body)).toEqual([
      { position: 'automatic', depth: 'full', confirm: true },
    ])
    expect(checked(linkedin)).toContain('Automatic')
    expect(await screen.findByRole('radiogroup', { name: 'How thorough' })).toBeInTheDocument()
  })

  it('lists only the quick checks, with no profile-visit notes, at quick depth', async () => {
    renderCard(automation({ linkedin: { depth: 'quick' } }))
    fireEvent.click((await group('LinkedIn automation')).getByRole('radio', { name: 'Automatic' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('to Automatic: quick checks')
    expect(
      within(dialog)
        .getAllByRole('listitem')
        .map((item) => item.textContent),
    ).toEqual(['checks for new connections', 'LinkedIn inbox checks'])
    expect(within(dialog).queryByRole('note')).toBeNull()
  })

  it('sends nothing when the dialog is cancelled', async () => {
    const { puts } = renderCard()
    const linkedin = await group('LinkedIn automation')
    fireEvent.click(linkedin.getByRole('radio', { name: 'Automatic' }))
    const dialog = await screen.findByRole('alertdialog')

    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))

    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(puts('linkedin')).toEqual([])
    expect(checked(linkedin)).toContain('Manual')
  })

  it('asks before adding the weekly full sync, and drops it in one click', async () => {
    const { puts } = renderCard(automation({ linkedin: { position: 'automatic', depth: 'quick' } }))
    const depth = within(await screen.findByRole('radiogroup', { name: 'How thorough' }))
    expect(depth.getByRole('radio', { name: 'Quick checks' })).toHaveAttribute(
      'aria-checked',
      'true',
    )

    fireEvent.click(depth.getByRole('radio', { name: 'Quick checks and a weekly full sync' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Add the weekly full sync?' })
    expect(
      within(dialog)
        .getAllByRole('listitem')
        .map((item) => item.textContent),
    ).toEqual(['a weekly full sync', 'profile refreshes'])
    expect(await within(dialog).findByRole('note', { name: 'Profile views' })).toBeInTheDocument()
    expect(puts('linkedin')).toEqual([])

    fireEvent.click(within(dialog).getByRole('button', { name: 'Add the full sync' }))
    await waitFor(() =>
      expect(
        depth.getByRole('radio', { name: 'Quick checks and a weekly full sync' }),
      ).toHaveAttribute('aria-checked', 'true'),
    )
    expect(puts('linkedin')[0]?.body).toEqual({
      position: 'automatic',
      depth: 'full',
      confirm: true,
    })

    fireEvent.click(depth.getByRole('radio', { name: 'Quick checks' }))
    await waitFor(() =>
      expect(depth.getByRole('radio', { name: 'Quick checks' })).toHaveAttribute(
        'aria-checked',
        'true',
      ),
    )
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(puts('linkedin')[1]?.body).toEqual({
      position: 'automatic',
      depth: 'quick',
      confirm: false,
    })
  })

  it('asks before Gmail Automatic and sends confirm: true', async () => {
    const { puts } = renderCard()
    const gmail = await group('Gmail automation')
    fireEvent.click(gmail.getByRole('radio', { name: 'Automatic' }))

    const dialog = await screen.findByRole('alertdialog', {
      name: 'Let netkeep send email on its own?',
    })
    expect(
      within(dialog)
        .getAllByRole('listitem')
        .map((item) => item.textContent),
    ).toEqual(['sends approved campaign email within sending hours', 'checks replies every 10 min'])
    expect(puts('gmail')).toEqual([])

    fireEvent.click(within(dialog).getByRole('button', { name: 'Turn on Automatic' }))
    await waitFor(() => expect(checked(gmail)).toContain('Automatic'))
    expect(puts('gmail').map((call) => call.body)).toEqual([
      { position: 'automatic', confirm: true },
    ])
  })

  it('disables Gmail Automatic while the server says it isn’t ready, and says why', async () => {
    const { puts } = renderCard(
      automation({ gmail: { automatic_ready: false, blocked_reason: BLOCKED_REASON } }),
    )
    const gmail = await group('Gmail automation')
    const stop = gmail.getByRole('radio', { name: 'Automatic' })
    expect(stop).toHaveAttribute('aria-disabled', 'true')
    expect(stop).toHaveAccessibleDescription(expect.stringContaining(BLOCKED_REASON))
    expect(screen.getByText(`${BLOCKED_REASON}.`)).toBeInTheDocument()

    fireEvent.click(stop)

    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(puts('gmail')).toEqual([])
    // Manual and Off still move.
    fireEvent.click(gmail.getByRole('radio', { name: 'Off' }))
    await waitFor(() => expect(checked(gmail)).toContain('Off'))
  })

  it('shows a 409 from a move to the left beside the switch', async () => {
    renderCard(automation(), {
      'PUT /api/v1/automation/gmail': () =>
        jsonResponse({ detail: 'Connect a Gmail mailbox first' }, 409),
    })
    const gmail = await group('Gmail automation')
    fireEvent.click(gmail.getByRole('radio', { name: 'Off' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Connect a Gmail mailbox first')
    expect(checked(gmail)).toContain('Manual')
  })

  it('shows a refused confirm in the dialog, which stays open', async () => {
    renderCard(automation(), {
      'PUT /api/v1/automation/gmail': () => jsonResponse({ detail: BLOCKED_REASON }, 409),
    })
    fireEvent.click((await group('Gmail automation')).getByRole('radio', { name: 'Automatic' }))
    const dialog = await screen.findByRole('alertdialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Turn on Automatic' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(BLOCKED_REASON)
    expect(screen.getByRole('alertdialog')).toBeInTheDocument()
  })

  it('works from the keyboard: arrows move left at once, and right asks first', async () => {
    const { puts } = renderCard()
    const linkedin = await group('LinkedIn automation')
    const manual = linkedin.getByRole('radio', { name: 'Manual' })
    // Tab reaches the checked stop only.
    expect(manual).toHaveAttribute('tabindex', '0')
    expect(linkedin.getByRole('radio', { name: 'Off' })).toHaveAttribute('tabindex', '-1')

    act(() => manual.focus())
    fireEvent.keyDown(manual, { key: 'ArrowRight' })
    const dialog = await screen.findByRole('alertdialog')
    expect(puts('linkedin')).toEqual([])
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())

    const back = linkedin.getByRole('radio', { name: 'Manual' })
    act(() => back.focus())
    fireEvent.keyDown(back, { key: 'ArrowLeft' })
    await waitFor(() => expect(checked(linkedin)).toContain('Off'))
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(puts('linkedin').map((call) => call.body)).toEqual([{ position: 'off', confirm: false }])
  })

  it('refetches the schedule, status, poll status and the top bar after a move', async () => {
    const { queryClient } = renderCard()
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    fireEvent.click((await group('LinkedIn automation')).getByRole('radio', { name: 'Off' }))
    await waitFor(() => expect(invalidate).toHaveBeenCalled())
    const keys = invalidate.mock.calls.map(([filters]) => filters?.queryKey)
    expect(keys).toEqual(
      expect.arrayContaining([
        linkedinKeys.schedule(),
        linkedinKeys.status(),
        pollStatusKeys.all,
        systemStatusKeys.all,
      ]),
    )
  })
})
