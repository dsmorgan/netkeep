/**
 * The LinkedIn queue and what waits for you (#383). Everyone here is invented;
 * nothing reaches LinkedIn or the backend: every request goes to a stand-in.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  Outlet,
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from '@tanstack/react-router'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { EventStreamContext } from '@/features/events/event-stream-context'
import {
  SCHEDULE_ARMED,
  STATUS_CLEAR,
  autoSendOutlook,
  backend,
  run,
  type Call,
  type Handler,
} from '@/features/linkedin/test-support'
import { FakeEventSource, resetFakeEventSource } from '@/test/fake-event-source'
import { jsonResponse, mockFetch } from '@/test/fetch'
import { formatWhen } from '@/features/campaigns/format'

import { readyQuery, waitingQuery } from './api'
import type { ReadyItem, ReadyPage, TryAgainItem, WaitingItem, WaitingPage } from './api'
import {
  AUTO_SEND_ON_NOTE,
  AUTO_SEND_STEP_OFF,
  AUTO_SEND_WAIT_TEXT,
  AUTO_SENT_STALE,
  ONE_AT_A_TIME,
  PREFILL_IT_YOURSELF,
  REVIEW_AND_SEND,
  WAITS_FOR_PREFILL,
  ageText,
  autoSendStepText,
  autoSendWaitText,
  reasonText,
  threadUrl,
} from './format'
import { LinkedInStepsSection } from './linkedin-steps-section'
import reasons from './prefill-reasons.json'
import {
  AUTO_SENDING,
  AUTO_SENT,
  AUTO_STAYS_DUE,
  AUTO_TYPED_NOT_SENT,
  AUTO_UNKNOWN,
  CLOSE_BUBBLE,
  FIRST_POLL_NOTE,
  MAYBE_CLOSE_BUBBLE,
  DRAFT_IN_BUBBLE,
  PARTLY_TYPED,
  PREFILL_NOTE,
  TYPED_WHOLE,
  CONFIRM_BUBBLE_BODY,
  CONFIRM_BUBBLE_LABEL,
  TRY_AGAIN_NOTE,
  TRY_AGAIN_STEP,
  TYPING,
  TYPING_WARNING,
  budgetText,
  confirmBubbleTitle,
  prefillEnding,
  prefillReason,
  prefillsLeftText,
} from './prefill-copy'

const HOUR = 3_600_000

function hoursAgo(hours: number): string {
  return new Date(Date.now() - hours * HOUR).toISOString()
}

function readyItem(overrides: Partial<ReadyItem> = {}): ReadyItem {
  return {
    enrollment_id: 31,
    campaign_id: 5,
    campaign_name: 'Autumn reconnect',
    step_position: 1,
    contact_id: 41,
    contact_name: 'Rosalind Quillfeather',
    due: hoursAgo(1),
    held_until: null,
    auto_send: false,
    ...overrides,
  }
}

function readyPage(items: ReadyItem[], tryAgain: TryAgainItem[] = []): ReadyPage {
  return { items, total: items.length, by_step: {}, try_again: tryAgain, prefills_left_today: 7 }
}

function tryAgainItem(
  overrides: Partial<TryAgainItem> = {},
  last: Partial<TryAgainItem['last_try']> = {},
): TryAgainItem {
  return {
    enrollment_id: 34,
    campaign_id: 5,
    campaign_name: 'Autumn reconnect',
    step_position: 1,
    contact_id: 44,
    contact_name: 'Wilhelmina Thorne',
    held_until: null,
    ...overrides,
    last_try: {
      reason: 'the Message control could not be clicked',
      tries: 1,
      run_id: 80,
      at: hoursAgo(1),
      click_attempted: true,
      budget_spent: true,
      counted_today: true,
      needs_confirmation: true,
      ...last,
    },
  }
}

function waitingItem(overrides: Partial<WaitingItem> = {}): WaitingItem {
  return {
    message_id: 71,
    status: 'prefilled',
    interrupted: false,
    partly_typed: false,
    enrollment_id: 32,
    campaign_id: 5,
    campaign_name: 'Autumn reconnect',
    contact_id: 42,
    contact_name: 'Tobias Marrowbone',
    prefilled_at: hoursAgo(2),
    not_sent_reason: null,
    auto_sent: false,
    ...overrides,
  }
}

function waitingPage(items: WaitingItem[]): WaitingPage {
  return { items, total: items.length }
}

const TWO_READY = readyPage([
  readyItem(),
  readyItem({ enrollment_id: 33, contact_id: 43, contact_name: 'Ada Pemberton' }),
])

function handlers(overrides: Record<string, Handler> = {}): Record<string, Handler> {
  return {
    'GET /api/v1/linkedin/status': () => jsonResponse(STATUS_CLEAR),
    'GET /api/v1/campaigns/linkedin/options': () => jsonResponse({ auto_send: false }),
    'GET /api/v1/campaigns/linkedin/ready': () => jsonResponse(TWO_READY),
    'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse(waitingPage([])),
    ...overrides,
  }
}

function renderSection(over: Record<string, Handler> = {}, campaignId?: number) {
  const calls: Call[] = []
  mockFetch(backend(handlers(over), calls))
  const source = new FakeEventSource('/api/v1/events')
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const root = createRootRoute({ component: () => <Outlet /> })
  const router = createRouter({
    routeTree: root.addChildren([
      createRoute({
        getParentRoute: () => root,
        path: '/',
        component: () => <LinkedInStepsSection campaignId={campaignId} />,
      }),
    ]),
    history: createMemoryHistory({ initialEntries: ['/'] }),
  })
  render(
    <EventStreamContext value={{ status: 'connected', source: source as unknown as EventSource }}>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </EventStreamContext>,
  )
  return { calls, source }
}

function posts(calls: Call[], path: string): Call[] {
  return calls.filter((call) => call.method === 'POST' && call.path === path)
}

const PREFILL = '/api/v1/campaigns/linkedin/prefill'

afterEach(() => {
  resetFakeEventSource()
})

describe('the LinkedIn queue', () => {
  it('lists due steps with a Prefill button each, and prefills nothing on its own', async () => {
    const { calls } = renderSection()

    const list = await screen.findByRole('list', { name: 'Ready to prefill' })
    const rows = within(list).getAllByRole('listitem')
    expect(rows).toHaveLength(2)
    expect(rows[0]).toHaveTextContent('Rosalind Quillfeather')
    expect(rows[0]).toHaveTextContent('Autumn reconnect')
    expect(rows[0]).toHaveTextContent('step 1')
    expect(
      within(rows[0]!).getByRole('button', { name: 'Prefill Rosalind Quillfeather' }),
    ).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Prefill next' })).toBeEnabled()
    expect(screen.getByText('Auto-send off')).toBeVisible()
    expect(posts(calls, PREFILL)).toEqual([])
  })

  it('starts the prefill a person clicks, then says it is typing in Chrome with live progress', async () => {
    let status = STATUS_CLEAR
    const { calls, source } = renderSection({
      'GET /api/v1/linkedin/status': () => jsonResponse(status),
      [`POST ${PREFILL}`]: () => {
        status = { ...STATUS_CLEAR, running_run_id: 81 }
        return jsonResponse({ enrollment_id: 31, message_id: 71, run_id: 81, task_id: 't' }, 202)
      },
      'GET /api/v1/linkedin/runs/81': () =>
        jsonResponse(run({ id: 81, kind: 'message_send', status: 'running' })),
    })

    fireEvent.click(await screen.findByRole('button', { name: 'Prefill Rosalind Quillfeather' }))

    expect(await screen.findByText(TYPING)).toBeVisible()
    expect(posts(calls, PREFILL).map((call) => call.body)).toEqual([
      { enrollment_id: 31, next: false, retry: false, no_bubble_open: false },
    ])
    // One at a time: every Prefill button is off while it types.
    for (const button of screen.getAllByRole('button', { name: /^Prefill/ })) {
      expect(button).toBeDisabled()
    }
    act(() => source.emit('run.progress', { run_id: 81, phase: 'typing', typed: 40 }))
    expect(await screen.findByText(/phase: typing/)).toBeVisible()
  })

  it('prefills the oldest with Prefill next', async () => {
    const { calls } = renderSection({
      [`POST ${PREFILL}`]: () =>
        jsonResponse({ enrollment_id: 31, message_id: 71, run_id: 81, task_id: 't' }, 202),
      'GET /api/v1/linkedin/runs/81': () =>
        jsonResponse(run({ id: 81, kind: 'message_send', status: 'running' })),
    })

    fireEvent.click(await screen.findByRole('button', { name: 'Prefill next' }))

    await waitFor(() =>
      expect(posts(calls, PREFILL).map((c) => c.body)).toEqual([
        { next: true, retry: false, no_bubble_open: false },
      ]),
    )
  })

  it('shows a refusal plainly, says nothing was typed, and offers no retry', async () => {
    renderSection({
      [`POST ${PREFILL}`]: () =>
        jsonResponse(
          {
            detail: {
              enrollment_id: 31,
              reasons: ['run_refused'],
              detail: 'no runner exists for message_send runs yet',
            },
          },
          409,
        ),
    })

    fireEvent.click(await screen.findByRole('button', { name: 'Prefill Rosalind Quillfeather' }))

    expect(await screen.findByText('Not prefilled. Nothing was typed in Chrome.')).toBeVisible()
    expect(screen.getByText(/netkeep couldn't start the prefill run/)).toBeVisible()
    expect(screen.getByText('no runner exists for message_send runs yet')).toBeVisible()
    expect(screen.queryByText(TYPING)).toBeNull()
    expect(screen.queryByRole('button', { name: /retry/i })).toBeNull()
    // Still nothing open: the buttons stay on for a person to try another.
    expect(screen.getByRole('button', { name: 'Prefill Ada Pemberton' })).toBeEnabled()
  })

  it('a refusal is an alert', async () => {
    renderSection({
      [`POST ${PREFILL}`]: () =>
        jsonResponse(
          { detail: { enrollment_id: 31, reasons: ['prefill_open'], detail: null } },
          409,
        ),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Prefill Rosalind Quillfeather' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Not prefilled. Nothing was typed in Chrome.',
    )
  })

  it('never says nothing was typed when the request itself failed (a 5xx)', async () => {
    renderSection({
      [`POST ${PREFILL}`]: () => jsonResponse({ detail: 'internal error' }, 500),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Prefill Rosalind Quillfeather' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(
      'The prefill request failed. Check Waiting for you and the LinkedIn page before you try again.',
    )
    expect(alert).not.toHaveTextContent('Nothing was typed')
  })

  it('never says nothing was typed when the network failed', async () => {
    renderSection({
      [`POST ${PREFILL}`]: () => {
        throw new TypeError('Failed to fetch')
      },
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Prefill Rosalind Quillfeather' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('The prefill request failed.')
    expect(alert).not.toHaveTextContent('Nothing was typed')
  })

  it('turns every Prefill button off while the prefill request is pending', async () => {
    const { calls } = renderSection({
      [`POST ${PREFILL}`]: () => new Promise<Response>(() => {}),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Prefill Rosalind Quillfeather' }))

    await waitFor(() => {
      for (const button of screen.getAllByRole('button', { name: /^Prefill/ })) {
        expect(button).toBeDisabled()
      }
    })
    fireEvent.click(screen.getByRole('button', { name: 'Prefill Ada Pemberton' }))
    expect(posts(calls, PREFILL)).toHaveLength(1)
  })

  it('keeps Prefill off when what waits for you cannot be read', async () => {
    const { calls } = renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse({ detail: 'boom' }, 500),
    })
    expect(await screen.findByText('What waits for you is unavailable.')).toBeVisible()
    for (const button of screen.getAllByRole('button', { name: /^Prefill/ })) {
      expect(button).toBeDisabled()
    }
    fireEvent.click(screen.getByRole('button', { name: 'Prefill Rosalind Quillfeather' }))
    expect(posts(calls, PREFILL)).toEqual([])
  })

  it('allows one prefill at a time: an open one turns every Prefill button off', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse(waitingPage([waitingItem()])),
    })

    expect(await screen.findByText(ONE_AT_A_TIME)).toBeVisible()
    await screen.findByRole('list', { name: 'Ready to prefill' })
    for (const button of screen.getAllByRole('button', { name: /^Prefill/ })) {
      expect(button).toBeDisabled()
    }
  })

  it('a stale message does not hold the slot', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () =>
        jsonResponse(waitingPage([waitingItem({ status: 'stale', prefilled_at: hoursAgo(80) })])),
    })

    await screen.findByRole('list', { name: 'Waiting for you' })
    expect(screen.queryByText(ONE_AT_A_TIME)).toBeNull()
    expect(screen.getByRole('button', { name: 'Prefill Rosalind Quillfeather' })).toBeEnabled()
  })

  it('says how a prefill run stopped, with no retry', async () => {
    let status = STATUS_CLEAR
    let current = run({ id: 81, kind: 'message_send', status: 'running' })
    const { source } = renderSection({
      'GET /api/v1/linkedin/status': () => jsonResponse(status),
      [`POST ${PREFILL}`]: () => {
        status = { ...STATUS_CLEAR, running_run_id: 81 }
        return jsonResponse({ enrollment_id: 31, message_id: 71, run_id: 81, task_id: 't' }, 202)
      },
      'GET /api/v1/linkedin/runs/81': () => jsonResponse(current),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Prefill Rosalind Quillfeather' }))
    await screen.findByText(TYPING)

    status = STATUS_CLEAR
    current = run({
      id: 81,
      kind: 'message_send',
      status: 'failed',
      stop_reason: 'composer_not_found',
      stop_reason_text: 'the message box did not open',
    })
    act(() => source.emit('run.finished', { run_id: 81, status: 'failed' }))

    expect(await screen.findByText('The prefill stopped.')).toBeVisible()
    expect(screen.getByText('the message box did not open.')).toBeVisible()
    expect(screen.getByText(/clear the composer in Chrome yourself/)).toBeVisible()
    expect(screen.queryByRole('button', { name: /retry/i })).toBeNull()
  })

  it("keeps a campaign's queue to that campaign, without Prefill next", async () => {
    const { calls } = renderSection({}, 5)

    await screen.findByRole('list', { name: 'Ready to prefill' })
    const ready = calls.find((call) => call.path === '/api/v1/campaigns/linkedin/ready')
    expect(ready?.query.get('campaign_id')).toBe('5')
    expect(screen.queryByRole('button', { name: 'Prefill next' })).toBeNull()
  })

  it('shows auto-send on as a highlighted note, not an alert, when the flag is on', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/options': () => jsonResponse({ auto_send: true }),
    })
    const badge = await screen.findByText('Auto-send on')
    expect(badge.closest('[role="note"]')).not.toBeNull()
    // It names the steps it applies to, not every step (#494).
    expect(badge.closest('[role="note"]')).toHaveTextContent(AUTO_SEND_ON_NOTE)
    expect(AUTO_SEND_ON_NOTE).toMatch(/for steps set to Auto-send/)
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('says what happens next on every row (#494)', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/options': () => jsonResponse({ auto_send: true }),
      'GET /api/v1/campaigns/linkedin/ready': () =>
        jsonResponse(
          readyPage([
            readyItem({ auto_send: true }),
            readyItem({ enrollment_id: 32, contact_name: 'Bartholomew Finch' }),
          ]),
        ),
    })
    const auto = await screen.findByRole('listitem', { name: 'Rosalind Quillfeather, step 1' })
    expect(auto).toHaveTextContent('Sends on its own')
    expect(auto).not.toHaveTextContent(WAITS_FOR_PREFILL)
    const prefill = screen.getByRole('listitem', { name: 'Bartholomew Finch, step 1' })
    expect(prefill).toHaveTextContent(WAITS_FOR_PREFILL)
    expect(prefill).not.toHaveTextContent(/Sends on its own/)
  })

  it('marks an auto-send step in the queue, sent on schedule while the flag is on', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/options': () => jsonResponse({ auto_send: true }),
      'GET /api/v1/campaigns/linkedin/ready': () =>
        jsonResponse(readyPage([readyItem({ auto_send: true })])),
    })
    const row = await screen.findByRole('listitem', { name: 'Rosalind Quillfeather, step 1' })
    expect(within(row).getByText('Auto-send')).toBeVisible()
    expect(row).toHaveTextContent('Sends on its own.')
    expect(row).toHaveTextContent(PREFILL_IT_YOURSELF)
    // A person can still prefill it; that never clicks Send.
    expect(within(row).getByRole('button', { name: 'Prefill Rosalind Quillfeather' })).toBeEnabled()
  })

  it('says when the next auto-send check sends the step', async () => {
    const outlook = autoSendOutlook({ enrollment_id: 31 })
    renderSection({
      'GET /api/v1/campaigns/linkedin/options': () => jsonResponse({ auto_send: true }),
      'GET /api/v1/linkedin/schedule': () =>
        jsonResponse({ ...SCHEDULE_ARMED, auto_send: outlook }),
      'GET /api/v1/campaigns/linkedin/ready': () =>
        jsonResponse(
          readyPage([
            readyItem({ auto_send: true }),
            readyItem({ auto_send: true, enrollment_id: 32, contact_name: 'Bartholomew Finch' }),
          ]),
        ),
    })
    const first = await screen.findByRole('listitem', { name: 'Rosalind Quillfeather, step 1' })
    await waitFor(() =>
      expect(first).toHaveTextContent(
        `Sends on its own at the next auto-send check, ${formatWhen(outlook.checked_at)}.`,
      ),
    )
    const second = screen.getByRole('listitem', { name: 'Bartholomew Finch, step 1' })
    expect(second).toHaveTextContent(
      'Sends on its own after the auto-send steps due before it, one at a time.',
    )
  })

  it('says why an auto-send step waits', async () => {
    const outlook = autoSendOutlook({
      wait: 'spacing',
      until: '2026-09-23T14:40:00Z',
      spacing_earliest: '2026-09-23T14:25:00Z',
      spacing_latest: '2026-09-23T14:50:00Z',
    })
    renderSection({
      'GET /api/v1/campaigns/linkedin/options': () => jsonResponse({ auto_send: true }),
      'GET /api/v1/linkedin/schedule': () =>
        jsonResponse({ ...SCHEDULE_ARMED, auto_send: outlook }),
      'GET /api/v1/campaigns/linkedin/ready': () =>
        jsonResponse(readyPage([readyItem({ auto_send: true })])),
    })
    const row = await screen.findByRole('listitem', { name: 'Rosalind Quillfeather, step 1' })
    await waitFor(() =>
      expect(row).toHaveTextContent(
        `Sends on its own, but waits. It waits out the spacing after the last LinkedIn message run. It ends at ${formatWhen('2026-09-23T14:40:00Z')}.`,
      ),
    )
  })

  it('says an auto-send step waits for a prefill while the flag is off', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/ready': () =>
        jsonResponse(readyPage([readyItem({ auto_send: true }), readyItem({ enrollment_id: 34 })])),
    })
    const rows = await screen.findAllByRole('listitem', { name: 'Rosalind Quillfeather, step 1' })
    expect(rows[0]).toHaveTextContent(AUTO_SEND_STEP_OFF)
    expect(within(rows[0]!).getByText('Auto-send step')).toBeVisible()
    expect(rows[1]).not.toHaveTextContent(/Auto-send/)
    expect(rows[1]).toHaveTextContent(WAITS_FOR_PREFILL)
  })

  it('shows the budget warning with auto-send on', async () => {
    const warning =
      'Auto-sent LinkedIn messages are set to 30 a day, above 20 a day. More LinkedIn messages a day make it more likely that LinkedIn restricts your account or asks you to verify it.'
    renderSection({
      'GET /api/v1/campaigns/linkedin/options': () =>
        jsonResponse({ auto_send: true, auto_send_warning: warning }),
    })
    const note = (await screen.findByText('Auto-send on')).closest('[role="note"]')
    expect(note).toHaveTextContent(warning)
  })

  it('says why auto-send is held and resumes it when you say the bubbles are closed', async () => {
    let held = true
    const { calls } = renderSection({
      'GET /api/v1/campaigns/linkedin/options': () =>
        jsonResponse({
          auto_send: true,
          auto_send_hold: held
            ? {
                reason: 'a message bubble is open in Chrome',
                since: hoursAgo(1),
                how_to_clear: 'Close every LinkedIn message bubble, then resume auto-send.',
              }
            : null,
        }),
      'POST /api/v1/campaigns/linkedin/auto-send/resume': () => {
        held = false
        return jsonResponse({ resumed: true })
      },
    })
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Auto-send is held: a message bubble is open in Chrome.')
    expect(alert).toHaveTextContent('Close every LinkedIn message bubble')
    fireEvent.click(within(alert).getByRole('button', { name: /resume auto-send/ }))
    await waitFor(() =>
      expect(posts(calls, '/api/v1/campaigns/linkedin/auto-send/resume')).toHaveLength(1),
    )
    expect(posts(calls, '/api/v1/campaigns/linkedin/auto-send/resume')[0]?.body).toEqual({
      confirm: true,
    })
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
  })

  it('says auto-send is unknown when the setting cannot be read', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/options': () => jsonResponse({ detail: 'boom' }, 500),
    })
    expect(await screen.findByText('Auto-send: unknown')).toBeVisible()
    expect(screen.queryByText('Auto-send off')).toBeNull()
  })
})

describe('waiting for you', () => {
  it('says why an auto-send typed a message but did not send it', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () =>
        jsonResponse(waitingPage([waitingItem({ not_sent_reason: 'the schedule is paused' })])),
    })
    const row = await screen.findByRole('listitem', { name: 'Tobias Marrowbone, prefilled' })
    expect(row).toHaveTextContent(
      "Auto-send typed it but didn't click Send: The LinkedIn schedule is paused, so netkeep sent nothing.",
    )
  })

  it('says a stale auto-sent message was never seen sent', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () =>
        jsonResponse(
          waitingPage([
            waitingItem({ status: 'stale', auto_sent: true, prefilled_at: hoursAgo(80) }),
          ]),
        ),
    })
    const row = await screen.findByRole('listitem', { name: 'Tobias Marrowbone, stale' })
    expect(within(row).getByText('Auto-sent')).toBeVisible()
    expect(row).toHaveTextContent(AUTO_SENT_STALE)
  })

  it('asks you to send a prefilled message yourself, with its age and both actions', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse(waitingPage([waitingItem()])),
    })

    const row = await screen.findByRole('listitem', { name: 'Tobias Marrowbone, prefilled' })
    expect(row).toHaveTextContent(REVIEW_AND_SEND)
    expect(row).toHaveTextContent('Prefilled 2 hours ago')
    expect(within(row).getByRole('button', { name: 'I sent it, check now' })).toBeEnabled()
    expect(within(row).getByRole('button', { name: 'Discard' })).toBeEnabled()
    expect(row).not.toHaveAttribute('data-state', 'stale')
  })

  it('tints the waiting list as the send tray only while something waits (#606)', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () =>
        jsonResponse(waitingPage([waitingItem({ status: 'stale', prefilled_at: hoursAgo(80) })])),
    })
    const list = await screen.findByRole('list', { name: 'Waiting for you' })
    const tray = list.closest('[data-slot="card"]')
    expect(tray).toHaveAttribute('data-tone', 'accent')
    expect(tray).toHaveClass('nk-tray')
  })

  it('leaves the waiting list plain when nothing waits (#606)', async () => {
    renderSection()
    await screen.findByText('Nothing waits for you.')
    const card = screen
      .getByRole('heading', { level: 2, name: 'Waiting for you' })
      .closest('[data-slot="card"]')
    expect(card).not.toHaveAttribute('data-tone')
    expect(card).not.toHaveClass('nk-tint')
  })

  it('highlights a stale message', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () =>
        jsonResponse(waitingPage([waitingItem({ status: 'stale', prefilled_at: hoursAgo(80) })])),
    })

    const row = await screen.findByRole('listitem', { name: 'Tobias Marrowbone, stale' })
    expect(row).toHaveAttribute('data-state', 'stale')
    expect(row.className).toMatch(/\bborder-warn\b/)
    expect(within(row).getByText('Stale')).toBeVisible()
    expect(row).toHaveTextContent('Prefilled 3 days ago')
  })

  it('treats a prefill over three days old as stale before the tick marks it', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () =>
        jsonResponse(waitingPage([waitingItem({ prefilled_at: hoursAgo(73) })])),
    })
    expect(await screen.findByRole('listitem', { name: 'Tobias Marrowbone, stale' })).toBeVisible()
  })

  it('offers only Discard for a prefill that stopped before it said what it typed', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () =>
        jsonResponse(
          waitingPage([
            waitingItem({ status: 'scheduled', interrupted: true, prefilled_at: null }),
          ]),
        ),
    })

    const row = await screen.findByRole('listitem', { name: 'Tobias Marrowbone, interrupted' })
    expect(row).toHaveTextContent(/clear it, then discard this/)
    expect(within(row).queryByRole('button', { name: 'I sent it, check now' })).toBeNull()
    expect(screen.getByText(ONE_AT_A_TIME)).toBeVisible()
  })

  it('"I sent it, check now" asks for an inbox poll and says what it does', async () => {
    const { calls } = renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse(waitingPage([waitingItem()])),
      'POST /api/v1/campaigns/linkedin/messages/71/check': () =>
        jsonResponse({ run_id: 91, task_id: 't' }, 202),
      'GET /api/v1/linkedin/runs/91': () =>
        jsonResponse(run({ id: 91, kind: 'inbox', status: 'running' })),
    })

    fireEvent.click(await screen.findByRole('button', { name: 'I sent it, check now' }))

    expect(await screen.findByText(/Checking your LinkedIn inbox/)).toBeVisible()
    expect(posts(calls, '/api/v1/campaigns/linkedin/messages/71/check')).toHaveLength(1)
  })

  it('shows a refused check', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse(waitingPage([waitingItem()])),
      'POST /api/v1/campaigns/linkedin/messages/71/check': () =>
        jsonResponse({ detail: 'outside active hours' }, 409),
    })

    fireEvent.click(await screen.findByRole('button', { name: 'I sent it, check now' }))

    expect(await screen.findByText('The inbox check did not start.')).toBeVisible()
    expect(screen.getByText('outside active hours')).toBeVisible()
  })

  it('discards only after you confirm, and refreshes the lists', async () => {
    let waiting = waitingPage([waitingItem()])
    const { calls } = renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse(waiting),
      'POST /api/v1/campaigns/linkedin/messages/71/discard': () => {
        waiting = waitingPage([])
        return jsonResponse({
          message_id: 71,
          status: 'discarded',
          enrollment_id: 32,
          enrollment_status: 'completed',
        })
      },
    })

    fireEvent.click(await screen.findByRole('button', { name: 'Discard' }))
    expect(posts(calls, '/api/v1/campaigns/linkedin/messages/71/discard')).toEqual([])
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('The step counts as fired')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Discard' }))

    expect(await screen.findByText('Nothing waits for you.')).toBeVisible()
    expect(posts(calls, '/api/v1/campaigns/linkedin/messages/71/discard')).toHaveLength(1)
    // The slot is free again.
    expect(screen.queryByText(ONE_AT_A_TIME)).toBeNull()
  })

  it("asks the server for one campaign's waiting list, and the queue checks every campaign's", async () => {
    const { calls } = renderSection(
      {
        'GET /api/v1/campaigns/linkedin/waiting': (call) =>
          jsonResponse(
            waitingPage(
              call.query.get('campaign_id') === '5'
                ? []
                : [waitingItem({ campaign_id: 6, contact_name: 'Other Person' })],
            ),
          ),
      },
      5,
    )
    expect(await screen.findByText('Nothing waits for you.')).toBeVisible()
    const waiting = calls.filter((c) => c.path === '/api/v1/campaigns/linkedin/waiting')
    expect(new Set(waiting.map((c) => c.query.get('campaign_id')))).toEqual(new Set(['5', null]))
    // An open prefill in another campaign still holds the one slot.
    expect(await screen.findByText(ONE_AT_A_TIME)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Prefill Rosalind Quillfeather' })).toBeDisabled()
  })

  it('frees "I sent it, check now" when the check run cannot be read', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse(waitingPage([waitingItem()])),
      'POST /api/v1/campaigns/linkedin/messages/71/check': () =>
        jsonResponse({ run_id: 91, task_id: 't' }, 202),
      'GET /api/v1/linkedin/runs/91': () => jsonResponse({ detail: 'boom' }, 500),
    })

    const button = await screen.findByRole('button', { name: 'I sent it, check now' })
    fireEvent.click(button)

    await screen.findByText(/Checking your LinkedIn inbox/) // the check started
    await waitFor(() => expect(button).toBeEnabled())
  })

  it('keeps "I sent it, check now" off while the check it started runs', async () => {
    let checkRun = run({ id: 91, kind: 'inbox', status: 'running' })
    const { calls, source } = renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse(waitingPage([waitingItem()])),
      'POST /api/v1/campaigns/linkedin/messages/71/check': () =>
        jsonResponse({ run_id: 91, task_id: 't' }, 202),
      'GET /api/v1/linkedin/runs/91': () => jsonResponse(checkRun),
    })

    const button = await screen.findByRole('button', { name: 'I sent it, check now' })
    fireEvent.click(button)
    await screen.findByText(/Checking your LinkedIn inbox/)
    expect(button).toBeDisabled()
    fireEvent.click(button)
    expect(posts(calls, '/api/v1/campaigns/linkedin/messages/71/check')).toHaveLength(1)

    checkRun = run({ id: 91, kind: 'inbox', status: 'completed' })
    act(() => source.emit('run.finished', { run_id: 91, status: 'completed' }))
    await waitFor(() => expect(button).toBeEnabled())
  })
})

describe('format', () => {
  it('builds the plain thread link from a stored conversation URN', () => {
    expect(threadUrl('urn:li:msg_conversation:2-INVENTED==')).toBe(
      'https://www.linkedin.com/messaging/thread/2-INVENTED%3D%3D/',
    )
    expect(threadUrl('urn:li:msg_conversation:(urn:li:fsd_profile:INVENTEDP,2-INVENTEDT)')).toBe(
      'https://www.linkedin.com/messaging/thread/2-INVENTEDT/',
    )
    expect(threadUrl(null)).toBeNull()
    expect(threadUrl('javascript:alert(1)')).toBeNull()
    expect(threadUrl('urn:li:member:123')).toBeNull()
  })

  it('says an age in plain words', () => {
    const now = new Date('2030-06-15T12:00:00Z')
    expect(ageText('2030-06-15T11:30:00Z', now)).toBe('30 minutes ago')
    expect(ageText('2030-06-15T09:00:00Z', now)).toBe('3 hours ago')
    expect(ageText('2030-06-12T12:00:00Z', now)).toBe('3 days ago')
    expect(ageText(null, now)).toBe('not recorded')
  })
})

/** Clicks Prefill for Rosalind, then ends the run the way a prefill run records it. */
async function finishPrefill(stop_reason: string, error: string | null, status = 'aborted') {
  let state = STATUS_CLEAR
  let current = run({ id: 81, kind: 'message_send', status: 'running' })
  const { source } = renderSection({
    'GET /api/v1/linkedin/status': () => jsonResponse(state),
    [`POST ${PREFILL}`]: () => {
      state = { ...STATUS_CLEAR, running_run_id: 81 }
      return jsonResponse({ enrollment_id: 31, message_id: 71, run_id: 81, task_id: 't' }, 202)
    },
    'GET /api/v1/linkedin/runs/81': () => jsonResponse(current),
  })
  fireEvent.click(await screen.findByRole('button', { name: 'Prefill Rosalind Quillfeather' }))
  await screen.findByText(TYPING)
  state = STATUS_CLEAR
  current = run({
    id: 81,
    kind: 'message_send',
    status: status as 'aborted',
    stop_reason,
    stop_reason_text: stop_reason,
    error,
  })
  act(() => source.emit('run.finished', { run_id: 81, status }))
  return screen.findByRole('alert')
}

describe('the prefill notes (#383, ADR 0007)', () => {
  it('tells you before the click that the recipient may see "typing…"', async () => {
    renderSection()
    await screen.findByRole('list', { name: 'Ready to prefill' })
    expect(screen.getByText(PREFILL_NOTE)).toBeVisible()
    expect(PREFILL_NOTE).toContain('may see "typing…"')
    expect(PREFILL_NOTE).toContain("Don't type or click in Chrome")
  })

  it('tells you again while it types, and asks you to leave Chrome alone', async () => {
    renderSection({
      [`POST ${PREFILL}`]: () =>
        jsonResponse({ enrollment_id: 31, message_id: 71, run_id: 81, task_id: 't' }, 202),
      'GET /api/v1/linkedin/runs/81': () =>
        jsonResponse(run({ id: 81, kind: 'message_send', status: 'running' })),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Prefill Rosalind Quillfeather' }))

    expect(await screen.findByText(TYPING_WARNING)).toBeVisible()
    expect(TYPING_WARNING).toContain('"typing…"')
    expect(screen.queryByText(PREFILL_NOTE)).toBeNull()
  })

  it('says the inbox is held before the first poll, and to run it by hand', async () => {
    renderSection({
      'GET /api/v1/poll-status': () =>
        jsonResponse({
          items: [
            {
              key: 'linkedin_inbox',
              group: 'linkedin',
              label: 'LinkedIn inbox',
              state: 'blocked',
              interval: 'PT2H',
              last_at: null,
              next_at: null,
              reason: 'The first LinkedIn inbox poll is run by hand',
              requested: false,
            },
          ],
          mailboxes: [],
        }),
    })

    const note = await screen.findByRole('note')
    expect(note).toHaveTextContent('held until it has')
    expect(note).toHaveTextContent('Run netkeep linkedin inbox by hand')
    expect(within(note).getByText('netkeep linkedin inbox').tagName).toBe('CODE')
    expect(note).not.toHaveTextContent('`')
    expect(note).toHaveTextContent(FIRST_POLL_NOTE)
    expect(within(note).getByRole('link', { name: 'Check inbox now' })).toHaveAttribute(
      'href',
      '/linkedin',
    )
  })

  it('shows no first-poll note once a poll has completed', async () => {
    renderSection({
      'GET /api/v1/poll-status': () =>
        jsonResponse({
          items: [
            {
              key: 'linkedin_inbox',
              group: 'linkedin',
              label: 'LinkedIn inbox',
              state: 'scheduled',
              interval: 'PT2H',
              last_at: hoursAgo(1),
              next_at: null,
              reason: null,
              requested: false,
            },
          ],
          mailboxes: [],
        }),
    })
    await screen.findByRole('list', { name: 'Ready to prefill' })
    expect(screen.queryByRole('note', { name: /./ })).toBeNull()
    expect(screen.queryByText(/has not read your LinkedIn inbox yet/)).toBeNull()
  })

  it('shows the hold as a refusal: held until the inbox is read, with the backend detail', async () => {
    renderSection({
      [`POST ${PREFILL}`]: () =>
        jsonResponse(
          {
            detail: {
              enrollment_id: 31,
              reasons: ['linkedin_inbox_stale'],
              detail:
                'the LinkedIn inbox has not been read: no poll has completed yet; run `netkeep linkedin inbox` by hand',
            },
          },
          409,
        ),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Prefill Rosalind Quillfeather' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Not prefilled. Nothing was typed in Chrome.')
    expect(alert).toHaveTextContent('held until the inbox is read')
    expect(alert).toHaveTextContent('run `netkeep linkedin inbox` by hand')
    expect(reasonText('linkedin_inbox_stale')).toMatch(/^held until the inbox is read/)
  })
})

describe('how a prefill that typed nothing ended', () => {
  it('after a refusal that followed the click, tells you to close the empty bubble', async () => {
    const alert = await finishPrefill('not_typed', 'recipient_name_mismatch')

    expect(alert).toHaveTextContent('Not prefilled. Nothing was typed in Chrome.')
    expect(alert).toHaveTextContent(
      "The name in the message bubble doesn't match the name on the profile.",
    )
    expect(alert).toHaveTextContent(CLOSE_BUBBLE)
    expect(alert).not.toHaveTextContent('recipient_name_mismatch')
    expect(screen.queryByRole('button', { name: /retry/i })).toBeNull()
  })

  it('does not ask you to close a bubble when the click never happened', async () => {
    const alert = await finishPrefill('not_typed', 'the contact has no public profile id to open')

    expect(alert).toHaveTextContent('no public LinkedIn profile address')
    expect(alert).not.toHaveTextContent('bubble')
  })

  it('says a refusal the run cannot place may have left a bubble open', async () => {
    const alert = await finishPrefill('not_typed', 'cancelled')
    expect(alert).toHaveTextContent('You cancelled the run.')
    expect(alert).toHaveTextContent(MAYBE_CLOSE_BUBBLE)
  })

  it('says a body that is too long typed nothing and parked the enrollment', async () => {
    const alert = await finishPrefill('too_long', 'the body is over the typing ceiling')
    expect(alert).toHaveTextContent('Nothing was typed in Chrome.')
    expect(alert).toHaveTextContent('too long to type')
    expect(alert).toHaveTextContent('parked the enrollment')
    expect(alert).not.toHaveTextContent('bubble')
  })

  it('shows a phrase it has no words for exactly as the backend wrote it', async () => {
    const alert = await finishPrefill('not_typed', 'a phrase from a newer netkeep')
    expect(alert).toHaveTextContent('a phrase from a newer netkeep')
    expect(alert).toHaveTextContent(MAYBE_CLOSE_BUBBLE)
  })
})

describe('the click recorded in the run (S3)', () => {
  async function finishWith(counts: Record<string, unknown> | null, error: string) {
    let state = STATUS_CLEAR
    let current = run({ id: 81, kind: 'message_send', status: 'running' })
    const { source } = renderSection({
      'GET /api/v1/linkedin/status': () => jsonResponse(state),
      [`POST ${PREFILL}`]: () => {
        state = { ...STATUS_CLEAR, running_run_id: 81 }
        return jsonResponse({ enrollment_id: 31, message_id: 71, run_id: 81, task_id: 't' }, 202)
      },
      'GET /api/v1/linkedin/runs/81': () => jsonResponse(current),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Prefill Rosalind Quillfeather' }))
    await screen.findByText(TYPING)
    state = STATUS_CLEAR
    current = run({
      id: 81,
      kind: 'message_send',
      status: 'aborted',
      stop_reason: 'not_typed',
      stop_reason_text: 'not_typed',
      error,
      counts,
    })
    act(() => source.emit('run.finished', { run_id: 81, status: 'aborted' }))
    return screen.findByRole('alert')
  }

  it('says a click that landed left a bubble open, whatever the phrase says', async () => {
    const alert = await finishWith(
      { message_click_attempted: true, message_clicked: true },
      'cancelled',
    )
    expect(alert).toHaveTextContent(CLOSE_BUBBLE)
  })

  it('says no bubble when no click was sent, even for a phrase that could mean one', async () => {
    const alert = await finishWith(
      { message_click_attempted: false, message_clicked: false },
      'the Message control could not be clicked',
    )
    expect(alert).not.toHaveTextContent('bubble')
  })

  it('says a bubble may be open when the click was sent but did not land', async () => {
    const alert = await finishWith(
      { message_click_attempted: true, message_clicked: false },
      'the profile opened somewhere else',
    )
    expect(alert).toHaveTextContent(MAYBE_CLOSE_BUBBLE)
  })

  it('falls back to the phrase table for an older run with no click counts', async () => {
    const alert = await finishWith({ typed_chars: 0 }, 'recipient_name_mismatch')
    expect(alert).toHaveTextContent(CLOSE_BUBBLE)
  })

  it('does not say the bubble is empty when it already held text, and gives that its own step', async () => {
    const alert = await finishWith(
      { message_click_attempted: true, message_clicked: true },
      'the composer is not empty',
    )
    expect(alert).toHaveTextContent(DRAFT_IN_BUBBLE)
    expect(alert).not.toHaveTextContent('It is empty')
    expect(alert).not.toHaveTextContent(CLOSE_BUBBLE)
  })
})

describe('a prefill that typed the whole message', () => {
  it('says plainly that it was typed', async () => {
    let state = STATUS_CLEAR
    let current = run({ id: 81, kind: 'message_send', status: 'running' })
    const { source } = renderSection({
      'GET /api/v1/linkedin/status': () => jsonResponse(state),
      [`POST ${PREFILL}`]: () => {
        state = { ...STATUS_CLEAR, running_run_id: 81 }
        return jsonResponse({ enrollment_id: 31, message_id: 71, run_id: 81, task_id: 't' }, 202)
      },
      'GET /api/v1/linkedin/runs/81': () => jsonResponse(current),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Prefill Rosalind Quillfeather' }))
    await screen.findByText(TYPING)
    state = STATUS_CLEAR
    current = run({ id: 81, kind: 'message_send', status: 'completed', stop_reason: 'prefilled' })
    act(() => source.emit('run.finished', { run_id: 81, status: 'completed' }))
    expect(await screen.findByText(TYPED_WHOLE)).toBeVisible()
    expect(TYPED_WHOLE).toMatch(/typed the message/)
  })
})

describe('a partly typed prefill in Waiting for you (B1)', () => {
  const PARTLY = waitingItem({
    status: 'failed',
    partly_typed: true,
    prefilled_at: null,
    message_id: 72,
  })

  it('shows a red row from the API, so it survives a reload, with only Discard', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse(waitingPage([PARTLY])),
    })

    const row = await screen.findByRole('listitem', { name: /Tobias Marrowbone, partly_typed/ })
    expect(row).toHaveAttribute('data-state', 'partly_typed')
    expect(row.className).toMatch(/destructive/)
    expect(row).toHaveTextContent(PARTLY_TYPED)
    expect(row).toHaveTextContent("Don't click Send")
    expect(row).toHaveTextContent('If you finished and sent it yourself, discard this.')
    expect(within(row).queryByRole('button', { name: 'I sent it, check now' })).toBeNull()
    expect(within(row).getByRole('button', { name: 'I cleared it, discard' })).toBeVisible()
    expect(within(row).getAllByRole('button')).toHaveLength(1)
  })

  it('holds the one-prefill slot: every Prefill button is off', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse(waitingPage([PARTLY])),
    })
    expect(await screen.findByText(ONE_AT_A_TIME)).toBeVisible()
    for (const button of screen.getAllByRole('button', { name: /^Prefill/ })) {
      expect(button).toBeDisabled()
    }
  })

  it('discards after you confirm that you cleared it', async () => {
    let waiting = waitingPage([PARTLY])
    const { calls } = renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse(waiting),
      'POST /api/v1/campaigns/linkedin/messages/72/discard': () => {
        waiting = waitingPage([])
        return jsonResponse({ message_id: 72, status: 'discarded' })
      },
    })
    fireEvent.click(await screen.findByRole('button', { name: 'I cleared it, discard' }))
    const dialog = await screen.findByRole('alertdialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'I cleared it, discard' }))

    await waitFor(() =>
      expect(posts(calls, '/api/v1/campaigns/linkedin/messages/72/discard')).toHaveLength(1),
    )
    expect(await screen.findByText('Nothing waits for you.')).toBeVisible()
  })
})

describe('every recorded phrase has words (S1 drift guard)', () => {
  // prefill-reasons.json is checked against the backend's source by
  // tests/test_prefill_reason_phrases.py.
  it.each(reasons)('%s', (phrase) => {
    const sample = phrase.replace(
      '{}',
      /^(after typing|before Send)/.test(phrase) ? 'the composer does not hold focus' : 'x',
    )
    const words = prefillReason(sample)
    expect(words.text).not.toBe(sample)
    expect(words.text).toMatch(/^(netkeep |[A-Z])/)
    expect(words.text).not.toMatch(/_/)
  })

  it('reads an auto-send refusal before Send through the phrase it carries', () => {
    const words = prefillReason('before Send: the composer does not hold focus')
    expect(words.text).toBe('Before Send, the message box lost focus, so netkeep stopped.')
    expect(prefillReason('the composer is not in one message form').text).toMatch(/Send button/)
    expect(prefillEnding('send_clicked', null)).toBeNull()
  })

  it('places each click-stage refusal', () => {
    for (const phrase of [
      "the tab is not on the contact's profile",
      'the tab left the profile before the click',
      'the Message control could not be read',
      'no Message control is visible',
      'no Message control is on screen with nothing over it; close or move what covers it',
      'whether a message bubble is open could not be read',
      'whether a message bubble is open could not be read',
      'a message bubble is already open in this or another LinkedIn tab in the netkeep Chrome window, minimized ones included; close it, then try again',
    ]) {
      expect(prefillReason(phrase).bubble).toBe('closed')
    }
    expect(prefillReason('the Message control could not be clicked').bubble).toBe('maybe')
    expect(prefillReason('the Message control was already clicked').bubble).toBe('maybe')
  })

  it('has words for the engine reasons that had none', () => {
    for (const reason of ['disconnected', 'campaign_at_cap', 'campaign_blocked']) {
      expect(reasonText(reason)).not.toBe(reason.replace(/_/g, ' '))
    }
  })
})

describe('how a prefill that typed part of the message ended', () => {
  it.each([
    ['partially_typed', 'The prefill stopped part of the way through typing.'],
    ['unknown', "netkeep can't tell how much it typed"],
  ])('%s says part of the message may be in the composer', async (outcome, title) => {
    const alert = await finishPrefill(outcome, 'the composer does not hold focus', 'failed')

    expect(alert).toHaveTextContent(title)
    expect(alert).toHaveTextContent('The message box lost focus, so netkeep stopped.')
    expect(alert).toHaveTextContent(PARTLY_TYPED)
    expect(alert).toHaveTextContent('never clears the composer and never retries')
    expect(alert).not.toHaveTextContent('Nothing was typed')
    expect(screen.queryByRole('button', { name: /retry/i })).toBeNull()
  })

  it('says it checked after typing when the final check failed', async () => {
    const alert = await finishPrefill(
      'partially_typed',
      "after typing: the composer's text changed",
      'failed',
    )
    expect(alert).toHaveTextContent(
      'After typing, the text in the message box changed while netkeep typed.',
    )
  })
})

describe('prefill refusal reasons in plain words', () => {
  // Every phrase the prefill records (browser.py, page_messaging.py, messaging.py,
  // message_send.py): none reaches the person raw.
  const PHRASES: readonly string[] = [
    'no claim',
    'the claim lapsed',
    'the message has no body',
    'the contact has no usable LinkedIn URN',
    'the typing plan refused the body (MultilineRefused)',
    'the body is over the typing ceiling',
    'the LinkedIn session is flagged',
    'heat is too high',
    "today's LinkedIn budget is spent",
    'the contact has no public profile id to open',
    'the page answered logged_out',
    'the profile opened somewhere else',
    'no Message control on the page',
    "a Message control opens something other than this contact's compose",
    'the Message control was not clicked',
    'no compose option was loaded after the click',
    'the compose option could not be read',
    'more than one compose option was loaded',
    'the compose option was not answered',
    'the compose option came from another path',
    "the compose option's URN has an unexpected shape",
    'the compose option names another profile',
    "the compose option's answer could not be read",
    "the compose option's answer is not JSON",
    "the compose option's answer has an unexpected shape",
    'the compose option names another recipient, or more than one',
    'a reply compose option names no conversation',
    'a new-message compose option names a conversation',
    "the compose option's type is not one the prefill knows",
    'another_compose',
    'recipient_name_mismatch',
    'recipient_name_unreadable',
    'no message composer is on the page',
    'a message bubble is already open in this or another LinkedIn tab in the netkeep Chrome window, minimized ones included; close it, then try again',
    'another message composer is on the page, in an open or minimized bubble, in this or another LinkedIn tab in the netkeep Chrome window; close the other bubbles',
    'another message bubble is on the page, open or minimized, in this or another LinkedIn tab in the netkeep Chrome window; close the others',
    'no Message control is on screen with nothing over it; close or move what covers it',
    "the conversation's bubble is not open",
    'the page shows a new-message bubble, not the conversation',
    "the composer is not in the conversation's bubble",
    "the bubble's header does not name one person",
    'the bubble is for someone else',
    'the contact has no public profile id to check the new-message bubble by',
    'the new-message bubble is not open, or more than one is',
    "the page shows the conversation's bubble, not a new message",
    'the composer is not in the new-message bubble',
    'the new-message bubble does not name exactly one recipient',
    "the new-message bubble's recipient field is missing",
    'the new-message bubble is for someone else',
    'the new-message bubble links to more than one person',
    'the new-message bubble links to no profile',
    "the tab's url changed",
    'the tab or the browser went away',
    'the composer is not empty',
    "the composer's text changed",
    'the composer does not hold focus',
    'the composer could not be read',
    'the bubble was not drawn',
    'a key call failed',
    'cancelled',
    'interrupted',
    'the prefill failed (RuntimeError)',
    'no tab with a clicked Message control',
    'the plan holds a control character',
    'the plan is empty',
  ]

  it.each(PHRASES)('says %j in plain words', (phrase) => {
    const reason = prefillReason(phrase)
    expect(reason.text).not.toBe(phrase)
    expect(reason.text).toMatch(/^(netkeep |[A-Z])/)
    expect(reason.text).not.toMatch(/_/)
  })

  it('names each of the three codes the way the maintainer would say it', () => {
    expect(prefillReason('recipient_name_mismatch').text).toBe(
      "The name in the message bubble doesn't match the name on the profile.",
    )
    expect(prefillReason('recipient_name_unreadable').text).toBe(
      "netkeep couldn't read the recipient's name in the message bubble, so it couldn't check it against the profile.",
    )
    expect(prefillReason('another_compose').text).toMatch(/second message composer/)
  })

  it("tells the new-message card's three refusals apart (#481)", () => {
    const words = [
      'the new-message bubble is for someone else',
      'the new-message bubble links to more than one person',
      'the new-message bubble links to no profile',
    ].map((phrase) => prefillReason(phrase))
    expect(words.map((w) => w.text)).toEqual([
      'The message bubble is for someone else.',
      "The message bubble links to more than one person, so netkeep couldn't confirm it's for this contact.",
      "The message bubble doesn't link to a profile, so netkeep couldn't confirm it's for this contact.",
    ])
    expect(words.every((w) => w.bubble === 'open')).toBe(true)
    expect(
      prefillReason('before Send: the new-message bubble links to more than one person').text,
    ).toBe(
      "Before Send, the message bubble links to more than one person, so netkeep couldn't confirm it's for this contact.",
    )
  })

  it('says only the refusals after the click leave a bubble open', () => {
    expect(prefillReason('the profile opened somewhere else').bubble).toBe('closed')
    expect(prefillReason('the claim lapsed').bubble).toBe('closed')
    expect(prefillReason('another_compose').bubble).toBe('open')
    expect(prefillReason("the tab's url changed").bubble).toBe('open')
    expect(prefillReason('cancelled').bubble).toBe('maybe')
  })

  it('has no ending for a prefill, or a stop that is not a prefill outcome', () => {
    expect(prefillEnding('prefilled', null)).toBeNull()
    expect(prefillEnding('session_flagged', null)).toBeNull()
    expect(prefillEnding(null, null)).toBeNull()
  })

  it('shows a prefill outcome in the runs list as words, not a code', async () => {
    const { stopReasonLabel } = await import('@/features/linkedin/fields')
    expect(
      stopReasonLabel({ stop_reason: 'partially_typed', stop_reason_text: 'partially_typed' }),
    ).toBe('part of the message was typed')
    expect(
      stopReasonLabel({ stop_reason: 'throttled', stop_reason_text: 'LinkedIn throttled it' }),
    ).toBe('LinkedIn throttled it')
  })
})

describe('the inbox check states (#433)', () => {
  it('says the first poll is run by hand when "I sent it, check now" is refused for it', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse(waitingPage([waitingItem()])),
      'POST /api/v1/campaigns/linkedin/messages/71/check': () =>
        jsonResponse({ run_id: 91, task_id: 't' }, 202),
      'GET /api/v1/linkedin/runs/91': () =>
        jsonResponse(
          run({
            id: 91,
            kind: 'inbox',
            status: 'failed',
            stop_reason: 'first_inbox_poll',
            stop_reason_text: 'refused: the first LinkedIn inbox poll is run by hand',
          }),
        ),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'I sent it, check now' }))
    expect(
      await screen.findByText(
        /The inbox check stopped: refused: the first LinkedIn inbox poll is run by hand/,
      ),
    ).toBeVisible()
  })

  it('says to run inbox-forget-owner when the check stops on another mailbox', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse(waitingPage([waitingItem()])),
      'POST /api/v1/campaigns/linkedin/messages/71/check': () =>
        jsonResponse({ run_id: 91, task_id: 't' }, 202),
      'GET /api/v1/linkedin/runs/91': () =>
        jsonResponse(
          run({
            id: 91,
            kind: 'inbox',
            status: 'failed',
            stop_reason: 'owner_mismatch',
            stop_reason_text:
              "the page showed another LinkedIn mailbox than this account's; if Chrome is signed in to another LinkedIn account, sign back in to yours; otherwise click **Forget inbox owner** under Posture on the Settings page, or run `netkeep linkedin inbox-forget-owner` (or, if your own contact has a LinkedIn ID, it does not match this mailbox and netkeep cannot edit it yet)",
          }),
        ),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'I sent it, check now' }))
    expect(await screen.findByText(/netkeep linkedin inbox-forget-owner/)).toBeVisible()
  })
})

describe('Try again (#445)', () => {
  const RETRY_ACCEPTED = () =>
    jsonResponse({ enrollment_id: 34, message_id: 72, run_id: 82, task_id: 't' }, 202)

  it('lists a step that typed nothing apart, with why, its tries and the budget', async () => {
    const { calls } = renderSection({
      'GET /api/v1/campaigns/linkedin/ready': () =>
        jsonResponse(readyPage([readyItem()], [tryAgainItem({}, { tries: 2 })])),
    })
    const list = await screen.findByRole('list', { name: 'Try again' })
    const row = within(list).getByRole('listitem')
    expect(row).toHaveTextContent('Wilhelmina Thorne')
    expect(row).toHaveTextContent("Chrome didn't take netkeep's click on Message.")
    expect(row).toHaveTextContent('Tried 2 times in a row.')
    expect(row).toHaveTextContent("That try used one of today's LinkedIn prefills.")
    expect(screen.getByText(TRY_AGAIN_NOTE)).toBeVisible()
    expect(screen.getByText(prefillsLeftText(7))).toBeVisible()
    // Only Try again claims it: no Prefill button for it, and nothing posted on its own.
    expect(screen.queryByRole('button', { name: 'Prefill Wilhelmina Thorne' })).toBeNull()
    expect(
      within(row).getByRole('button', { name: 'Try again for Wilhelmina Thorne' }),
    ).toBeEnabled()
    expect(posts(calls, PREFILL)).toEqual([])
  })

  it('retries at once when the last try never clicked Message', async () => {
    const { calls } = renderSection({
      'GET /api/v1/campaigns/linkedin/ready': () =>
        jsonResponse(
          readyPage(
            [],
            [
              tryAgainItem(
                {},
                {
                  reason: 'the browser was busy',
                  click_attempted: null,
                  budget_spent: false,
                  counted_today: false,
                  needs_confirmation: false,
                },
              ),
            ],
          ),
        ),
      [`POST ${PREFILL}`]: RETRY_ACCEPTED,
      'GET /api/v1/linkedin/runs/82': () =>
        jsonResponse(run({ id: 82, kind: 'message_send', status: 'running' })),
    })
    const row = within(await screen.findByRole('list', { name: 'Try again' })).getByRole('listitem')
    expect(row).toHaveTextContent("didn't use one of today's LinkedIn prefills")
    fireEvent.click(within(row).getByRole('button', { name: 'Try again for Wilhelmina Thorne' }))
    await waitFor(() =>
      expect(posts(calls, PREFILL).map((c) => c.body)).toEqual([
        { enrollment_id: 34, next: false, retry: true, no_bubble_open: false },
      ]),
    )
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(await screen.findByText(TYPING)).toBeVisible()
  })

  it('asks before a retry whose last try clicked Message, and posts nothing until you confirm', async () => {
    const { calls } = renderSection({
      'GET /api/v1/campaigns/linkedin/ready': () => jsonResponse(readyPage([], [tryAgainItem()])),
      [`POST ${PREFILL}`]: RETRY_ACCEPTED,
      'GET /api/v1/linkedin/runs/82': () =>
        jsonResponse(run({ id: 82, kind: 'message_send', status: 'running' })),
    })
    const button = await screen.findByRole('button', { name: 'Try again for Wilhelmina Thorne' })

    fireEvent.click(button)
    let dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent(confirmBubbleTitle('Wilhelmina Thorne'))
    expect(dialog).toHaveTextContent(CONFIRM_BUBBLE_BODY)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(posts(calls, PREFILL)).toEqual([])

    fireEvent.click(button)
    dialog = await screen.findByRole('alertdialog')
    fireEvent.click(within(dialog).getByRole('button', { name: CONFIRM_BUBBLE_LABEL }))
    await waitFor(() =>
      expect(posts(calls, PREFILL).map((c) => c.body)).toEqual([
        { enrollment_id: 34, next: false, retry: true, no_bubble_open: true },
      ]),
    )
  })

  it('keeps Try again off while another prefill is open, or the sending hours hold it', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/ready': () =>
        jsonResponse(
          readyPage(
            [],
            [
              tryAgainItem(),
              tryAgainItem({
                enrollment_id: 35,
                contact_id: 45,
                contact_name: 'Ada Pemberton',
                held_until: new Date(Date.now() + HOUR).toISOString(),
              }),
            ],
          ),
        ),
      'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse(waitingPage([waitingItem()])),
    })
    expect(
      await screen.findByRole('button', { name: 'Try again for Wilhelmina Thorne' }),
    ).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Try again for Ada Pemberton' })).toBeDisabled()
    expect(screen.getByText(ONE_AT_A_TIME)).toBeVisible()
  })

  it('shows a refused retry like any refusal', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/ready': () =>
        jsonResponse(readyPage([], [tryAgainItem({}, { needs_confirmation: false })])),
      [`POST ${PREFILL}`]: () =>
        jsonResponse(
          { detail: { enrollment_id: 34, reasons: ['browser_out_of_budget'], detail: null } },
          409,
        ),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Try again for Wilhelmina Thorne' }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Not prefilled. Nothing was typed in Chrome.')
    expect(alert).toHaveTextContent("today's LinkedIn prefill budget is spent")
  })

  it('says in words whether the last try counted against today', () => {
    expect(budgetText({ budget_spent: true, counted_today: true })).toBe(
      "That try used one of today's LinkedIn prefills.",
    )
    expect(budgetText({ budget_spent: true, counted_today: false })).toMatch(/earlier day/)
    expect(budgetText({ budget_spent: false, counted_today: false })).toMatch(/didn't use/)
    expect(budgetText({ budget_spent: null, counted_today: null })).toMatch(/can't tell/)
    expect(prefillsLeftText(0)).toBe("Today's LinkedIn prefill budget is spent.")
    expect(prefillsLeftText(1)).toBe("1 of today's LinkedIn prefills is left.")
  })

  it('points a not_typed ending at Try again, and never a part-typed or too long one', () => {
    expect(prefillEnding('not_typed', 'the browser was busy')?.steps).toContain(TRY_AGAIN_STEP)
    for (const reason of ['partially_typed', 'unknown', 'too_long']) {
      expect(prefillEnding(reason, 'x')?.steps).not.toContain(TRY_AGAIN_STEP)
    }
  })

  it('says each new refusal in plain words', () => {
    for (const reason of ['try_again_needed', 'nothing_to_retry', 'confirm_no_bubble']) {
      expect(reasonText(reason)).not.toBe(reason.replace(/_/g, ' '))
    }
  })
})

describe('the auto-send outlook in words', () => {
  it('has words for every reason, with no issue or decision numbers', () => {
    for (const wait of Object.keys(AUTO_SEND_WAIT_TEXT)) {
      const text = autoSendWaitText(autoSendOutlook({ wait }))
      expect(text).toMatch(/\.$/)
      expect(text).not.toMatch(/#\d|ADR/)
    }
  })

  it('names the hold, the inbox poll, and when a wait ends', () => {
    expect(
      autoSendWaitText(
        autoSendOutlook({ wait: 'held', hold_reason: 'a tab is left open in Chrome' }),
      ),
    ).toBe('Auto-send is held: a tab is left open in Chrome.')
    expect(
      autoSendWaitText(
        autoSendOutlook({ wait: 'inbox_stale', inbox_age_hours: 8, inbox_limit_hours: 6.83 }),
      ),
    ).toBe(
      "The LinkedIn inbox hasn't been read recently: the last complete poll was 8.0 hours ago, and the limit is 6.8 hours.",
    )
    expect(autoSendWaitText(autoSendOutlook({ wait: 'inbox_stale' }))).toBe(
      "The LinkedIn inbox hasn't been read recently: no inbox poll has completed yet.",
    )
    const until = '2026-09-24T00:00:00Z'
    expect(autoSendWaitText(autoSendOutlook({ wait: 'budget_spent', until }))).toBe(
      `Today's auto-send budget is spent. It resets at ${formatWhen(until)}.`,
    )
    expect(autoSendWaitText(autoSendOutlook({ wait: 'outside_active_hours', until }))).toBe(
      `It's outside your LinkedIn active hours. They next open at ${formatWhen(until)}.`,
    )
    expect(autoSendWaitText(autoSendOutlook({ wait: 'heat', until }))).toBe(
      `Heat is at or over its threshold. It cools by ${formatWhen(until)}.`,
    )
    expect(autoSendWaitText(autoSendOutlook({ wait: 'sending_hours', until }))).toBe(
      `Your sending hours hold every due auto-send step. They open at ${formatWhen(until)}.`,
    )
    expect(autoSendWaitText(autoSendOutlook())).toBeNull()
  })

  it('says a spilled step waits for its own time of day', () => {
    const until = '2026-09-24T18:00:00Z'
    expect(autoSendWaitText(autoSendOutlook({ wait: 'spilled', until }))).toBe(
      `Your sending hours are any time, so each due auto-send step waits for its own time of day. The first goes at ${formatWhen(until)}.`,
    )
  })

  it('takes the spacing bounds from the API', () => {
    const text = autoSendWaitText(
      autoSendOutlook({
        wait: 'spacing',
        until: '2026-09-23T14:40:00Z',
        spacing_earliest: '2026-09-23T14:25:00Z',
        spacing_latest: '2026-09-23T14:50:00Z',
        spacing_min_minutes: 15,
        spacing_max_minutes: 40,
      }),
    )
    expect(text).toContain('The spacing is 15 to 40 minutes')
  })

  it('gives an overdue check its catch-up window, never "now"', () => {
    const outlook = autoSendOutlook({
      enrollment_id: 9,
      catch_up: true,
      checked_at: '2026-09-23T14:05:00Z',
      catch_up_latest: '2026-09-23T14:20:00Z',
    })
    const step = { enrollment_id: 9, due: '2026-09-23T12:00:00Z' }
    expect(autoSendStepText(outlook, step, new Date('2026-09-23T14:00:00Z'))).toBe(
      `Sends on its own at the next auto-send check, between ${formatWhen('2026-09-23T14:05:00Z')} and ${formatWhen('2026-09-23T14:20:00Z')}.`,
    )
  })

  it('says a step not due yet goes once it is due', () => {
    const now = new Date('2026-09-23T14:00:00Z')
    const step = { enrollment_id: 9, due: '2026-09-23T18:00:00Z' }
    expect(autoSendStepText(autoSendOutlook({ wait: 'no_step_due' }), step, now)).toBe(
      'Sends on its own once it is due.',
    )
    expect(autoSendStepText(null, step, now)).toBe('Sends on its own.')
  })
})

describe('when the page lists only part of a queue (#508)', () => {
  function attentionWith(tryAgain: number) {
    return {
      checked_at: '2026-10-14T15:00:00Z',
      counts: {
        inbox: 0,
        dashboard: tryAgain,
        waiting: 0,
        try_again: tryAgain,
        triage: 0,
        imports: 0,
      },
      flags: { dashboard: null, gmail: null, linkedin: null, settings: null },
      activity: { state: 'idle', label: 'Idle.', run_id: null, run_kind: null },
      title_total: tryAgain,
    }
  }
  const retries = Array.from({ length: 50 }, (_, n) =>
    tryAgainItem({ enrollment_id: 100 + n, contact_id: 200 + n }),
  )

  it('says how many wait for you when there are more than the page holds', async () => {
    const items = Array.from({ length: 50 }, (_, n) =>
      waitingItem({ message_id: 300 + n, enrollment_id: 400 + n, contact_id: 500 + n }),
    )
    renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse({ items, total: 64 }),
    })

    expect(await screen.findByText('Showing 50 of 64, oldest first.')).toBeVisible()
  })

  it('says nothing more when the page holds them all', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse(waitingPage([waitingItem()])),
    })

    await screen.findByRole('list', { name: 'Waiting for you' })
    expect(screen.queryByText(/^Showing \d+ of/)).toBeNull()
  })

  it('says how many wait for Try again on the dashboard, from the nav count', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/ready': () => jsonResponse(readyPage([], retries)),
      'GET /api/v1/attention': () => jsonResponse(attentionWith(73)),
    })

    expect(await screen.findByText('Showing 50 of 73, oldest try first.')).toBeVisible()
  })

  it('says nothing more for Try again when the list holds every item', async () => {
    // A count a moment newer than the list (a retry since) never makes a short list look cut off.
    renderSection({
      'GET /api/v1/campaigns/linkedin/ready': () =>
        jsonResponse(readyPage([], retries.slice(0, 3))),
      'GET /api/v1/attention': () => jsonResponse(attentionWith(73)),
    })

    await screen.findByRole('list', { name: 'Try again' })
    expect(screen.queryByText(/oldest try first/)).toBeNull()
  })

  it('leaves the note off a campaign page, whose list is that campaign’s alone', async () => {
    const { calls } = renderSection(
      {
        'GET /api/v1/campaigns/linkedin/ready': () => jsonResponse(readyPage([], retries)),
        'GET /api/v1/attention': () => jsonResponse(attentionWith(73)),
      },
      5,
    )

    await screen.findByRole('list', { name: 'Try again' })
    expect(screen.queryByText(/oldest try first/)).toBeNull()
    expect(calls.some((call) => call.path === '/api/v1/attention')).toBe(false)
  })
})

describe('an auto-send is not a prefill (#704)', () => {
  const autoSend = (over: Parameters<typeof run>[0] = {}) =>
    run({ id: 90, kind: 'message_send', trigger: 'scheduled', status: 'running', ...over })

  it('says netkeep is sending, not that a message waits for you, while an auto-send runs', async () => {
    renderSection({
      'GET /api/v1/linkedin/status': () => jsonResponse({ ...STATUS_CLEAR, running_run_id: 90 }),
      'GET /api/v1/linkedin/runs/90': () => jsonResponse(autoSend()),
    })

    expect(await screen.findByText(AUTO_SENDING)).toBeVisible()
    expect(screen.queryByText(TYPING)).toBeNull()
    expect(screen.queryByText(PREFILL_NOTE)).toBeNull()
    for (const button of screen.getAllByRole('button', { name: /^Prefill/ })) {
      expect(button).toBeDisabled()
    }
  })

  it('still calls a manual prefill a prefill', async () => {
    renderSection({
      'GET /api/v1/linkedin/status': () => jsonResponse({ ...STATUS_CLEAR, running_run_id: 90 }),
      'GET /api/v1/linkedin/runs/90': () => jsonResponse(autoSend({ trigger: 'manual' })),
    })

    expect(await screen.findByText(TYPING)).toBeVisible()
    expect(screen.queryByText(AUTO_SENDING)).toBeNull()
  })

  it('says an auto-send clicked Send when it ends, never that it waits for you', async () => {
    let state: typeof STATUS_CLEAR = { ...STATUS_CLEAR, running_run_id: 90 } as never
    let current = autoSend()
    const { source } = renderSection({
      'GET /api/v1/linkedin/status': () => jsonResponse(state),
      'GET /api/v1/linkedin/runs/90': () => jsonResponse(current),
    })
    await screen.findByText(AUTO_SENDING)

    state = STATUS_CLEAR
    current = autoSend({ status: 'completed', stop_reason: 'send_clicked' })
    act(() => source.emit('run.finished', { run_id: 90, status: 'completed' }))

    expect(await screen.findByText(AUTO_SENT)).toBeVisible()
    expect(screen.queryByText(TYPED_WHOLE)).toBeNull()
  })

  async function endAutoSend(ended: NonNullable<Parameters<typeof run>[0]>) {
    let state = { ...STATUS_CLEAR, running_run_id: 90 } as typeof STATUS_CLEAR
    let current = autoSend()
    const { source } = renderSection({
      'GET /api/v1/linkedin/status': () => jsonResponse(state),
      'GET /api/v1/linkedin/runs/90': () => jsonResponse(current),
    })
    await screen.findByText(AUTO_SENDING)
    state = STATUS_CLEAR
    current = autoSend(ended)
    act(() => source.emit('run.finished', { run_id: 90, status: ended.status }))
  }

  it('says the message may already be sent when Send could not be confirmed (unknown)', async () => {
    await endAutoSend({ status: 'failed', stop_reason: 'unknown' })

    expect(await screen.findByText(AUTO_UNKNOWN)).toBeVisible()
    expect(screen.getByText('Check whether it was sent.')).toBeVisible()
    // An `unknown` run may have stopped partway through typing.
    expect(AUTO_UNKNOWN).not.toMatch(/typed the whole message/)
    expect(AUTO_UNKNOWN).toMatch(/only part of it may be in the composer/)
    expect(screen.queryByText(PARTLY_TYPED)).toBeNull()
    expect(screen.queryByText(/can't tell how much it typed/)).toBeNull()
  })

  it('says an auto-send that typed but did not click Send waits for you', async () => {
    await endAutoSend({ status: 'completed', stop_reason: 'prefilled' })

    expect(await screen.findByText(AUTO_TYPED_NOT_SENT)).toBeVisible()
    expect(screen.queryByText(TYPED_WHOLE)).toBeNull()
  })

  it('calls an auto-send that typed nothing an auto-send, not a prefill', async () => {
    await endAutoSend({ status: 'aborted', stop_reason: 'not_typed', counts: { opened: true } })

    expect(await screen.findByText('The auto-send stopped.')).toBeVisible()
    expect(screen.queryByText(/Not prefilled/)).toBeNull()
  })

  it('says an auto-send that opened nothing stays due and tries again by itself', async () => {
    await endAutoSend({
      status: 'aborted',
      stop_reason: 'not_typed',
      error: 'the Message control could not be clicked',
      counts: { opened: false, typed_chars: 0 },
    })

    expect(await screen.findByText(AUTO_STAYS_DUE)).toBeVisible()
    expect(screen.queryByText(TRY_AGAIN_STEP)).toBeNull()
  })

  it('sends a person-cancelled auto-send that opened nothing to Try again', async () => {
    await endAutoSend({
      status: 'aborted',
      stop_reason: 'not_typed',
      counts: { opened: false, typed_chars: 0 },
      cancel_requested_at: '2026-09-23T10:01:00Z',
    })

    expect(await screen.findByText(TRY_AGAIN_STEP)).toBeVisible()
    expect(screen.queryByText(AUTO_STAYS_DUE)).toBeNull()
  })

  it('calls any other auto-send stop an auto-send', async () => {
    await endAutoSend({ status: 'failed', stop_reason: 'session_flagged' })

    expect(await screen.findByText('The auto-send stopped.')).toBeVisible()
    expect(screen.queryByText('The prefill stopped.')).toBeNull()
  })

  it("hides the last run's note while a new auto-send runs", async () => {
    let state = { ...STATUS_CLEAR, running_run_id: 90 } as typeof STATUS_CLEAR
    let current = autoSend()
    const { source } = renderSection({
      'GET /api/v1/linkedin/status': () => jsonResponse(state),
      'GET /api/v1/linkedin/runs/90': () => jsonResponse(current),
      'GET /api/v1/linkedin/runs/91': () => jsonResponse(autoSend({ id: 91 })),
    })
    await screen.findByText(AUTO_SENDING)
    state = STATUS_CLEAR
    current = autoSend({ status: 'completed', stop_reason: 'send_clicked' })
    act(() => source.emit('run.finished', { run_id: 90, status: 'completed' }))
    await screen.findByText(AUTO_SENT)

    state = { ...STATUS_CLEAR, running_run_id: 91 } as typeof STATUS_CLEAR
    act(() => source.emit('run.started', { run_id: 91, kind: 'message_send' }))

    await waitFor(() => expect(screen.getByText(AUTO_SENDING)).toBeVisible())
    expect(screen.queryByText(AUTO_SENT)).toBeNull()
  })
})

describe('the queue refreshes on its own (#704)', () => {
  it('reloads the ready and waiting lists every minute', () => {
    expect(readyQuery().refetchInterval).toBe(60_000)
    expect(waitingQuery().refetchInterval).toBe(60_000)
  })

  it('turns Prefill on when a hold lapses, with no new data', async () => {
    vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] })
    try {
      const held = new Date(Date.now() + 20_000).toISOString()
      renderSection({
        'GET /api/v1/campaigns/linkedin/ready': () =>
          jsonResponse(readyPage([readyItem({ held_until: held })])),
      })
      const button = await screen.findByRole('button', { name: 'Prefill Rosalind Quillfeather' })
      expect(button).toBeDisabled()

      act(() => {
        // Past the hold but short of the 60 s refetch: only the clock moves the card.
        vi.advanceTimersByTime(30_000)
      })
      await waitFor(() =>
        expect(screen.getByRole('button', { name: 'Prefill Rosalind Quillfeather' })).toBeEnabled(),
      )
    } finally {
      vi.useRealTimers()
    }
  })

  it('marks a waiting message stale when the clock passes three days, with no new data', async () => {
    vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] })
    try {
      const prefilledAt = new Date(Date.now() - 72 * HOUR + 20_000).toISOString()
      renderSection({
        'GET /api/v1/campaigns/linkedin/waiting': () =>
          jsonResponse(waitingPage([waitingItem({ prefilled_at: prefilledAt })])),
      })
      const row = await screen.findByRole('listitem', { name: /^Tobias Marrowbone/ })
      expect(row).not.toHaveAttribute('data-state', 'stale')

      act(() => {
        vi.advanceTimersByTime(30_000)
      })
      await waitFor(() =>
        expect(screen.getByRole('listitem', { name: /^Tobias Marrowbone/ })).toHaveAttribute(
          'data-state',
          'stale',
        ),
      )
    } finally {
      vi.useRealTimers()
    }
  })
})
