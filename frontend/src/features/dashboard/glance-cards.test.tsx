import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  Outlet,
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from '@tanstack/react-router'
import { fireEvent, render, screen, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { formatWhen } from '@/features/linkedin/fields'
import { autoSendOutlook } from '@/features/linkedin/test-support'
import { jsonResponse, mockFetch } from '@/test/fetch'

import { CampaignsCard, ChangedJobsCard, ComingUpCard, RepliesCard } from './glance-cards'

type Route = () => Response | Promise<Response>

/** A promise the test never resolves, to hold a query in its loading state. */
const never: Route = () => new Promise<Response>(() => {})
const broken: Route = () => jsonResponse({ detail: 'boom' }, 500)
const json =
  (body: unknown): Route =>
  () =>
    jsonResponse(body)

/**
 * One card under a minimal router (its `Link`s need one), against a fake
 * backend keyed by path. A path the test did not route answers 500, so a card
 * reading something unexpected shows up as an error state, not a pass.
 */
function renderCard(Card: () => ReactNode, routes: Record<string, Route>) {
  const seen: string[] = []
  mockFetch((request) => {
    const { pathname } = new URL(request.url)
    seen.push(`${pathname}${new URL(request.url).search}`)
    const route = routes[pathname]
    return route === undefined ? jsonResponse({ detail: `unexpected ${pathname}` }, 500) : route()
  })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const root = createRootRoute({ component: () => <Outlet /> })
  const router = createRouter({
    routeTree: root.addChildren([
      createRoute({ getParentRoute: () => root, path: '/', component: Card }),
      createRoute({
        getParentRoute: () => root,
        path: '/contacts/$contactId',
        component: () => <p>Contact page</p>,
      }),
      createRoute({
        getParentRoute: () => root,
        path: '/linkedin',
        component: () => <p>LinkedIn page</p>,
      }),
      createRoute({
        getParentRoute: () => root,
        path: '/campaigns',
        component: () => <p>Campaigns page</p>,
      }),
      createRoute({
        getParentRoute: () => root,
        path: '/campaigns/$campaignId',
        component: () => <p>Campaign page</p>,
      }),
    ]),
    history: createMemoryHistory({ initialEntries: ['/'] }),
  })
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return { seen }
}

/** The card that holds `title`, to read inside it alone. */
async function card(title: string) {
  const heading = await screen.findByRole('heading', { level: 2, name: title })
  const node = heading.closest('[data-slot="card"]')
  if (!(node instanceof HTMLElement)) throw new Error(`no card around ${title}`)
  return within(node)
}

afterEach(() => {
  vi.restoreAllMocks()
})

// --- fixtures (fake people only) ---------------------------------------------------------

const FUTURE = '2099-01-02T15:00:00Z'
const PAST = '2020-01-02T15:00:00Z'

function fire(overrides: Record<string, unknown> = {}) {
  return {
    enrollment_id: 1,
    due: FUTURE,
    campaign_id: 7,
    campaign_name: 'Autumn hello',
    step_position: 2,
    channel: 'email',
    contact_id: 42,
    contact_name: 'Fic Person',
    ...overrides,
  }
}

const SCHEDULE = '/api/v1/linkedin/schedule'

/** An armed schedule with no jobs, for the cards that read only its auto-send outlook. */
function scheduleWith(overrides: Record<string, unknown> = {}) {
  return { armed: true, armed_at: null, scheduler_running: true, jobs: [], ...overrides }
}
// --- coming up: campaign sends ----------------------------------------------------------

const FIRES = '/api/v1/dashboard/next-fires'

/** Scheduled runs off, so a send test reads only its sends. */
const UNARMED = json(scheduleWith({ armed: false }))
const NO_SENDS = json({ items: [], total: 0 })

/**
 * Coming up against both its reads, each defaulting to an answer with nothing in it,
 * so a test names only the one it is about.
 */
function renderComingUp(routes: Record<string, Route>) {
  return renderCard(ComingUpCard, { [FIRES]: NO_SENDS, [SCHEDULE]: UNARMED, ...routes })
}

describe('ComingUpCard: campaign sends', () => {
  const PATH = FIRES

  it('announces that it is checking', async () => {
    renderComingUp({ [PATH]: never })
    expect((await card('Coming up')).getByRole('status')).toHaveTextContent('Checking')
  })

  it('says so when nothing is scheduled', async () => {
    renderComingUp({ [PATH]: json({ items: [], total: 0 }) })
    const body = await card('Coming up')
    expect(await body.findByText('Nothing is scheduled.')).toBeInTheDocument()
  })

  it('shows a failure as an alert, not a crash', async () => {
    renderComingUp({ [PATH]: broken })
    const body = await card('Coming up')
    expect(await body.findByRole('alert')).toHaveTextContent('Upcoming sends could not be loaded.')
  })

  it('lists each send with its time, contact link, campaign, and step', async () => {
    renderComingUp({
      [PATH]: json({
        items: [
          fire({ enrollment_id: 1, due: PAST, contact_id: 41, contact_name: 'Ada Example' }),
          fire({ enrollment_id: 2, due: FUTURE, step_position: 3 }),
          fire({ enrollment_id: 3, channel: 'linkedin', ready_to_prefill: true }),
        ],
        total: 5,
      }),
    })
    const body = await card('Coming up')
    const items = await body.findAllByRole('listitem')
    expect(items).toHaveLength(3)
    expect(items[0]).toHaveTextContent('due now')
    expect(items[0]).toHaveTextContent('Autumn hello · step 2')
    expect(within(items[0]!).getByRole('link', { name: 'Ada Example' })).toHaveAttribute(
      'href',
      '/contacts/41',
    )
    expect(items[1]).not.toHaveTextContent('due now')
    expect(items[1]).toHaveTextContent('2099')
    expect(items[1]).toHaveTextContent('step 3')
    expect(items[1]).not.toHaveTextContent('ready to prefill')
    expect(items[2]).toHaveTextContent('Autumn hello · step 2 · LinkedIn, ready to prefill')
    expect(body.getByText('and 2 more')).toBeInTheDocument()
  })

  it('says an auto-send step sends on its own, and when', async () => {
    const outlook = autoSendOutlook({ enrollment_id: 3 })
    renderComingUp({
      [PATH]: json({
        items: [
          fire({
            enrollment_id: 3,
            due: PAST,
            channel: 'linkedin',
            ready_to_prefill: true,
            auto_send: true,
          }),
        ],
        total: 1,
      }),
      [SCHEDULE]: json(scheduleWith({ auto_send: outlook })),
    })
    const body = await card('Coming up')
    expect(await body.findByText(/LinkedIn, sends on its own/)).toBeInTheDocument()
    expect(
      await body.findByText(
        `Sends on its own at the next auto-send check, ${formatWhen(outlook.checked_at)}.`,
      ),
    ).toBeInTheDocument()
    expect(body.queryByText(/ready to prefill/)).not.toBeInTheDocument()
  })

  it('says why an auto-send step waits', async () => {
    const until = '2099-01-03T00:00:00Z'
    renderComingUp({
      [PATH]: json({
        items: [fire({ due: PAST, channel: 'linkedin', ready_to_prefill: true, auto_send: true })],
        total: 1,
      }),
      [SCHEDULE]: json(
        scheduleWith({ auto_send: autoSendOutlook({ wait: 'budget_spent', until }) }),
      ),
    })
    const body = await card('Coming up')
    expect(
      await body.findByText(
        `Sends on its own, but waits. Today's auto-send budget is spent. It resets at ${formatWhen(until)}.`,
      ),
    ).toBeInTheDocument()
  })

  it('keeps ready to prefill for an auto-send step while auto-send is off', async () => {
    renderComingUp({
      [PATH]: json({
        items: [fire({ due: PAST, channel: 'linkedin', ready_to_prefill: true, auto_send: true })],
        total: 1,
      }),
      [SCHEDULE]: json(scheduleWith()),
    })
    const body = await card('Coming up')
    expect(await body.findByText(/LinkedIn, ready to prefill/)).toBeInTheDocument()
    expect(body.queryByText(/sends on its own/i)).not.toBeInTheDocument()
  })
})

// --- coming up: scheduled LinkedIn runs -------------------------------------------------

describe('ComingUpCard: scheduled LinkedIn runs', () => {
  function schedule(overrides: Record<string, unknown> = {}) {
    return {
      armed: true,
      armed_at: '2026-09-20T12:00:00Z',
      scheduler_running: true,
      jobs: [
        { kind: 'connections_full', interval_hours: 168, next_due: '2099-03-01T09:00:00Z' },
        { kind: 'connections_incremental', interval_hours: 24, next_due: '2099-01-01T09:00:00Z' },
        { kind: 'enrich', interval_hours: 24, next_due: null },
      ],
      ...overrides,
    }
  }

  it('announces that it is checking', async () => {
    renderComingUp({ [SCHEDULE]: never })
    expect((await card('Coming up')).getByRole('status')).toHaveTextContent('Checking')
  })

  it('says scheduled runs are off rather than showing a time that will not fire', async () => {
    renderComingUp({ [SCHEDULE]: json(schedule({ armed: false })) })
    const body = await card('Coming up')
    expect(await body.findByText(/Scheduled runs are off/)).toBeInTheDocument()
    expect(body.queryByText(/2099/)).not.toBeInTheDocument()
    expect(body.getByRole('link', { name: 'Open LinkedIn' })).toHaveAttribute(
      'href',
      '/linkedin#schedule',
    )
  })

  it('says so when armed but nothing is scheduled yet', async () => {
    renderComingUp({
      [SCHEDULE]: json(
        schedule({ jobs: [{ kind: 'enrich', interval_hours: 24, next_due: null }] }),
      ),
    })
    const body = await card('Coming up')
    expect(await body.findByText('Nothing is scheduled.')).toBeInTheDocument()
  })

  it('shows a failure as an alert', async () => {
    renderComingUp({ [SCHEDULE]: broken })
    const body = await card('Coming up')
    expect(await body.findByRole('alert')).toHaveTextContent(
      'The LinkedIn schedule could not be loaded.',
    )
  })

  it('lists the due runs soonest first, leaving out a kind with no due time', async () => {
    renderComingUp({ [SCHEDULE]: json(schedule()) })
    const body = await card('Coming up')
    const items = await body.findAllByRole('listitem')
    expect(items.map((item) => item.textContent)).toEqual([
      expect.stringContaining('Incremental connections sync'),
      expect.stringContaining('Full connections sync'),
    ])
    expect(body.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('warns when no scheduler runs in this process', async () => {
    renderComingUp({
      [SCHEDULE]: json(schedule({ scheduler_running: false })),
    })
    const body = await card('Coming up')
    expect(await body.findByRole('alert')).toHaveTextContent('No scheduler is running')
  })

  it('says the schedule is paused instead of showing times that will not fire (#324)', async () => {
    let paused = true
    const { seen } = renderComingUp({
      [SCHEDULE]: () =>
        jsonResponse(
          schedule(paused ? { paused: true, paused_at: '2026-09-21T08:00:00Z' } : { paused }),
        ),
      [`${SCHEDULE}/unpause`]: () => {
        paused = false
        return jsonResponse(schedule({ paused: false, paused_at: null }))
      },
    })
    const body = await card('Coming up')
    expect(await body.findByText(/no new scheduled run starts/)).toBeInTheDocument()
    expect(body.getByText(/nothing skipped is replayed/)).toBeInTheDocument()
    expect(body.queryByText(/2099/)).not.toBeInTheDocument()

    fireEvent.click(body.getByRole('button', { name: 'Unpause schedule' }))
    expect(await body.findByRole('button', { name: 'Pause schedule' })).toBeInTheDocument()
    expect(seen).toContain(`${SCHEDULE}/unpause`)
    expect((await body.findAllByRole('listitem')).length).toBe(2)
  })

  it('says both when the schedule is disarmed and paused (#324)', async () => {
    renderComingUp({
      [SCHEDULE]: json(schedule({ armed: false, paused: true, paused_at: '2026-09-21T08:00:00Z' })),
    })
    const body = await card('Coming up')
    expect(await body.findByText(/Scheduled runs are off/)).toBeInTheDocument()
    expect(body.getByRole('link', { name: 'Open LinkedIn' })).toHaveAttribute(
      'href',
      '/linkedin#schedule',
    )
    expect(body.getByText(/no new scheduled run starts/)).toBeInTheDocument()
    expect(body.getByRole('button', { name: 'Unpause schedule' })).toBeInTheDocument()
    expect(body.queryByText(/2099/)).not.toBeInTheDocument()
  })

  it('offers to pause an armed schedule (#324)', async () => {
    const { seen } = renderComingUp({
      [SCHEDULE]: json(schedule({ paused: false, paused_at: null })),
      [`${SCHEDULE}/pause`]: json(schedule({ paused: true, paused_at: '2026-09-21T08:00:00Z' })),
    })
    const body = await card('Coming up')
    fireEvent.click(await body.findByRole('button', { name: 'Pause schedule' }))
    expect(await body.findByText(/no new scheduled run starts/)).toBeInTheDocument()
    expect(seen).toContain(`${SCHEDULE}/pause`)
  })

  it('lists the auto-send check with its time and what it would do', async () => {
    const outlook = autoSendOutlook({ next_fire: '2099-02-01T09:00:00Z' })
    renderComingUp({ [SCHEDULE]: json(schedule({ auto_send: outlook })) })
    const body = await card('Coming up')
    const items = await body.findAllByRole('listitem')
    expect(items.map((item) => item.textContent)).toEqual([
      expect.stringContaining('Incremental connections sync'),
      expect.stringContaining('Auto-send check'),
      expect.stringContaining('Full connections sync'),
    ])
    expect(items[1]).toHaveTextContent(formatWhen('2099-02-01T09:00:00Z'))
    expect(items[1]).toHaveTextContent('It sends the oldest due auto-send step.')
  })

  it('says in plain words why the auto-send check would send nothing', async () => {
    const until = '2099-02-01T10:00:00Z'
    const outlook = autoSendOutlook({ next_fire: '2099-02-01T09:00:00Z', wait: 'heat', until })
    renderComingUp({ [SCHEDULE]: json(schedule({ auto_send: outlook })) })
    const body = await card('Coming up')
    expect(
      await body.findByText(`Heat is at or over its threshold. It cools by ${formatWhen(until)}.`),
    ).toBeInTheDocument()
  })

  it('shows an overdue check at its catch-up window, not its old time', async () => {
    const outlook = autoSendOutlook({
      next_fire: '2020-01-01T09:00:00Z',
      catch_up: true,
      checked_at: '2099-02-01T09:05:00Z',
      catch_up_latest: '2099-02-01T09:20:00Z',
    })
    renderComingUp({ [SCHEDULE]: json(schedule({ auto_send: outlook })) })
    const body = await card('Coming up')
    const row = (await body.findAllByRole('listitem')).find((item) =>
      item.textContent?.includes('Auto-send check'),
    )
    expect(row).toHaveTextContent(formatWhen('2099-02-01T09:05:00Z'))
    expect(row).not.toHaveTextContent(formatWhen('2020-01-01T09:00:00Z'))
    expect(row).toHaveTextContent("It's overdue, so it runs once between")
  })

  it('reads nothing when the outlook is unavailable', async () => {
    renderComingUp({ [SCHEDULE]: json(schedule({ auto_send: null })) })
    const body = await card('Coming up')
    await body.findAllByRole('listitem')
    expect(body.queryByText(/Auto-send check/)).not.toBeInTheDocument()
  })

  it('says when the auto-send check is not scheduled yet', async () => {
    const outlook = autoSendOutlook({ next_fire: null, wait: 'not_scheduled' })
    renderComingUp({ [SCHEDULE]: json(schedule({ auto_send: outlook })) })
    const body = await card('Coming up')
    expect(await body.findByText(/isn't scheduled yet/)).toBeInTheDocument()
  })

  it('leaves the auto-send check out while auto-send is off', async () => {
    renderComingUp({
      [SCHEDULE]: json(
        schedule({ auto_send: autoSendOutlook({ on: false, wait: 'auto_send_off' }) }),
      ),
    })
    const body = await card('Coming up')
    await body.findAllByRole('listitem')
    expect(body.queryByText(/Auto-send check/)).not.toBeInTheDocument()
  })
})

// --- coming up: one timeline ----------------------------------------------------------

describe('ComingUpCard: one timeline', () => {
  const jobs = [
    { kind: 'connections_incremental', interval_hours: 24, next_due: '2099-01-01T09:00:00Z' },
    { kind: 'enrich', interval_hours: 24, next_due: '2099-01-03T09:00:00Z' },
  ]

  it('puts the sends and the LinkedIn runs in one list, soonest first (#739)', async () => {
    renderComingUp({
      [FIRES]: json({
        items: [
          fire({ enrollment_id: 1, due: '2099-01-02T09:00:00Z', contact_name: 'Ada Example' }),
          fire({ enrollment_id: 2, due: '2099-01-04T09:00:00Z', contact_name: 'Bo Example' }),
        ],
        total: 2,
      }),
      [SCHEDULE]: json(scheduleWith({ jobs })),
    })
    const body = await card('Coming up')
    const items = await body.findAllByRole('listitem')
    expect(items.map((item) => item.textContent)).toEqual([
      expect.stringContaining('Incremental connections sync'),
      expect.stringContaining('Ada Example'),
      expect.stringContaining('Enrichment'),
      expect.stringContaining('Bo Example'),
    ])
  })

  it('still lists the sends while the schedule is paused, and none of its runs', async () => {
    renderComingUp({
      [FIRES]: json({ items: [fire({ contact_name: 'Ada Example' })], total: 1 }),
      [SCHEDULE]: json(scheduleWith({ jobs, paused: true, paused_at: '2026-09-21T08:00:00Z' })),
    })
    const body = await card('Coming up')
    const items = await body.findAllByRole('listitem')
    expect(items).toHaveLength(1)
    expect(items[0]).toHaveTextContent('Ada Example')
    expect(body.getByRole('button', { name: 'Unpause schedule' })).toBeInTheDocument()
  })

  it('keeps the runs when the sends fail, and says which read failed', async () => {
    renderComingUp({ [FIRES]: broken, [SCHEDULE]: json(scheduleWith({ jobs })) })
    const body = await card('Coming up')
    expect(await body.findByRole('alert')).toHaveTextContent('Upcoming sends could not be loaded.')
    expect(body.getAllByRole('listitem')).toHaveLength(2)
  })
})

// --- campaigns ---------------------------------------------------------------------------

describe('CampaignsCard', () => {
  const PATH = '/api/v1/campaigns'
  const campaign = (overrides: Record<string, unknown> = {}) => ({
    id: 3,
    name: 'Autumn hello',
    status: 'active',
    steps: 2,
    enrollments: { active: 4, replied: 1 },
    mailbox_id: null,
    created_at: '2026-09-01T12:00:00Z',
    next_action_at: null,
    ...overrides,
  })

  it('announces that it is checking', async () => {
    renderCard(CampaignsCard, { [PATH]: never })
    expect((await card('Campaigns')).getByRole('status')).toHaveTextContent('Checking')
  })

  it('says so when there are none, and links to the campaigns page', async () => {
    renderCard(CampaignsCard, { [PATH]: json([]) })
    const body = await card('Campaigns')
    expect(await body.findByText('No campaigns yet.')).toBeInTheDocument()
    expect(body.getByRole('link', { name: 'All campaigns' })).toHaveAttribute('href', '/campaigns')
  })

  it('shows a failure as an alert', async () => {
    renderCard(CampaignsCard, { [PATH]: broken })
    const body = await card('Campaigns')
    expect(await body.findByRole('alert')).toHaveTextContent('Campaigns could not be loaded.')
  })

  it('links each campaign to its page, with its status and who is enrolled', async () => {
    renderCard(CampaignsCard, {
      [PATH]: json([
        campaign(),
        campaign({ id: 4, name: 'Founders check-in', status: 'reviewing', enrollments: {} }),
        ...[5, 6, 7, 8].map((id) => campaign({ id, name: `Campaign ${id}` })),
      ]),
    })
    const body = await card('Campaigns')
    const items = await body.findAllByRole('listitem')
    expect(items).toHaveLength(5)
    expect(within(items[0]!).getByRole('link', { name: 'Autumn hello' })).toHaveAttribute(
      'href',
      '/campaigns/3',
    )
    expect(items[0]).toHaveTextContent('Active')
    expect(items[0]).toHaveTextContent('4 active, 1 replied')
    expect(items[1]).toHaveTextContent('Reviewing')
    expect(items[1]).toHaveTextContent('nobody enrolled')
    expect(body.getByText('and 1 more')).toBeInTheDocument()
  })
})

// --- replies this week -------------------------------------------------------------------

describe('RepliesCard', () => {
  const PATH = '/api/v1/dashboard/inbound'
  const inbound = (count: number) => ({
    count,
    since: '2026-09-20T12:00:00Z',
    reply_detection: false,
  })

  it('announces that it is checking', async () => {
    renderCard(() => <RepliesCard />, { [PATH]: never })
    expect((await card('Replies this week')).getByRole('status')).toHaveTextContent('Checking')
  })

  it('says reply detection is not set up, and that nothing came in', async () => {
    renderCard(() => <RepliesCard />, { [PATH]: json(inbound(0)) })
    const body = await card('Replies this week')
    expect(await body.findByText(/Reply detection is not set up yet/)).toBeInTheDocument()
    expect(body.getByText('No inbound messages logged in the last 7 days.')).toBeInTheDocument()
  })

  it('shows a failure as an alert', async () => {
    renderCard(() => <RepliesCard />, { [PATH]: broken })
    const body = await card('Replies this week')
    expect(await body.findByRole('alert')).toHaveTextContent(
      'Inbound messages could not be loaded.',
    )
  })

  it('counts inbound messages under their own name, never as replies', async () => {
    renderCard(() => <RepliesCard />, { [PATH]: json(inbound(3)) })
    const body = await card('Replies this week')
    expect(await body.findByText(/inbound messages \(email and LinkedIn\)/)).toHaveTextContent(
      '3 inbound messages (email and LinkedIn) logged in the last 7 days.',
    )
    expect(body.queryByText(/\d+ repl/)).not.toBeInTheDocument()
  })

  // #606: the card is tinted green only when something came in.
  const repliesCard = () =>
    screen
      .getByRole('heading', { level: 2, name: 'Replies this week' })
      .closest('[data-slot="card"]')

  it('is tinted green when inbound messages came in', async () => {
    renderCard(() => <RepliesCard />, { [PATH]: json(inbound(3)) })
    await screen.findByText(/inbound messages \(email and LinkedIn\)/)
    expect(repliesCard()).toHaveAttribute('data-tone', 'ok')
  })

  it('stays plain when nothing came in', async () => {
    renderCard(() => <RepliesCard />, { [PATH]: json(inbound(0)) })
    await screen.findByText('No inbound messages logged in the last 7 days.')
    expect(repliesCard()).not.toHaveAttribute('data-tone')
  })

  it('stays plain when the count could not be loaded', async () => {
    renderCard(() => <RepliesCard />, { [PATH]: broken })
    await screen.findByText('Inbound messages could not be loaded.')
    expect(repliesCard()).not.toHaveAttribute('data-tone')
  })
})

// --- changed jobs ------------------------------------------------------------------------

describe('ChangedJobsCard', () => {
  const PATH = '/api/v1/dashboard/changed-jobs'

  it('announces that it is checking', async () => {
    renderCard(ChangedJobsCard, { [PATH]: never })
    expect((await card('Changed jobs')).getByRole('status')).toHaveTextContent('Checking')
  })

  it('says so when nobody changed jobs', async () => {
    renderCard(ChangedJobsCard, { [PATH]: json({ items: [], total: 0, days: 30 }) })
    const body = await card('Changed jobs')
    expect(
      await body.findByText('netkeep noticed no position changes in the last 30 days.'),
    ).toBeInTheDocument()
  })

  it('shows a failure as an alert', async () => {
    renderCard(ChangedJobsCard, { [PATH]: broken })
    const body = await card('Changed jobs')
    expect(await body.findByRole('alert')).toHaveTextContent('Changed jobs could not be loaded.')
  })

  it('links each person to their contact, with the new role and the date', async () => {
    renderCard(ChangedJobsCard, {
      [PATH]: json({
        items: [
          {
            contact_id: 12,
            contact_name: 'Fictional Mover',
            current_title: 'Head of Tea',
            current_company: 'Kettle Ltd',
            noticed_at: '2026-09-01T12:00:00Z',
          },
          {
            contact_id: 13,
            contact_name: 'Quiet Leaver',
            current_title: null,
            current_company: null,
            noticed_at: '2026-08-30T12:00:00Z',
          },
        ],
        total: 4,
        days: 30,
      }),
    })
    const body = await card('Changed jobs')
    const items = await body.findAllByRole('listitem')
    expect(within(items[0]!).getByRole('link', { name: 'Fictional Mover' })).toHaveAttribute(
      'href',
      '/contacts/12',
    )
    // Midday UTC is the same day in every zone a test runs in.
    expect(items[0]).toHaveTextContent('noticed Sep 1, 2026')
    expect(items[0]).toHaveTextContent('Head of Tea at Kettle Ltd')
    expect(within(items[1]!).getByRole('link', { name: 'Quiet Leaver' })).toHaveAttribute(
      'href',
      '/contacts/13',
    )
    expect(body.getByText('and 2 more')).toBeInTheDocument()
    expect(body.getByText('New positions netkeep noticed in the last 30 days.')).toBeInTheDocument()
  })
})
