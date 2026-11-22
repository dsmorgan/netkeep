import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { automation, configWith } from '@/features/automation/test-support'
import { jsonResponse, mockFetch } from '@/test/fetch'

import { ScheduleCard } from './schedule-card'
import { backend, BUDGET, SCHEDULE_ARMED, type Call, type Handler } from './test-support'

function renderCard(handlers: Record<string, Handler> = {}) {
  const calls: Call[] = []
  mockFetch(
    backend(
      {
        'GET /api/v1/automation': () => jsonResponse(automation()),
        'GET /api/v1/linkedin/schedule': () => jsonResponse(SCHEDULE_ARMED),
        'GET /api/v1/linkedin/budget': () => jsonResponse(BUDGET),
        'GET /api/v1/settings/config': () => jsonResponse(configWith()),
        ...handlers,
      },
      calls,
    ),
  )
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <ScheduleCard />
    </QueryClientProvider>,
  )
  return { calls }
}

describe('ScheduleCard (#829, #835)', () => {
  it('renders no switch: the page’s automation bar holds it', async () => {
    renderCard()
    expect(await screen.findByText(/Nothing runs on its own/)).toBeInTheDocument()
    expect(screen.queryByRole('radiogroup')).toBeNull()
    expect(screen.queryByRole('radio')).toBeNull()
    expect(screen.queryByRole('checkbox')).toBeNull()
    expect(screen.queryByRole('alertdialog')).toBeNull()
  })

  it('says nothing is scheduled on Manual, with no run times and no “LinkedIn: off”', async () => {
    renderCard()
    expect(
      await screen.findByText('Nothing runs on its own while LinkedIn automation is Manual.'),
    ).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Next runs' })).toBeNull()
    expect(screen.queryByText(/LinkedIn: off/)).toBeNull()
    // Pause is gone: Manual does its job.
    expect(screen.queryByRole('button', { name: /pause/i })).toBeNull()
  })

  it('names Off when the switch is Off', async () => {
    renderCard({
      'GET /api/v1/automation': () => jsonResponse(automation({ linkedin: { position: 'off' } })),
    })
    expect(
      await screen.findByText('Nothing runs on its own while LinkedIn automation is Off.'),
    ).toBeInTheDocument()
  })

  it('lists each kind’s next due time on Automatic, and the kinds quick checks skip', async () => {
    renderCard({
      'GET /api/v1/automation': () =>
        jsonResponse(automation({ linkedin: { position: 'automatic', depth: 'quick' } })),
    })
    const list = within(await screen.findByRole('list', { name: 'Next runs' }))
    const items = list.getAllByRole('listitem').map((item) => item.textContent)
    expect(items[0]).toMatch(/^Incremental connections sync: every 24h, next due /)
    expect(items[1]).toBe('Enrichment: not on quick checks')
    expect(screen.queryByText(/Nothing runs on its own/)).toBeNull()
  })

  it('follows the schedule’s skipped_on_quick flag, not a list of its own (#834)', async () => {
    renderCard({
      'GET /api/v1/automation': () =>
        jsonResponse(automation({ linkedin: { position: 'automatic', depth: 'quick' } })),
      'GET /api/v1/linkedin/schedule': () =>
        jsonResponse({
          ...SCHEDULE_ARMED,
          jobs: [
            // Flipped from today's kinds on purpose: the card must read the flag.
            {
              kind: 'enrich',
              interval_hours: 6,
              next_due: '2026-09-23T14:00:00Z',
              skipped_on_quick: false,
            },
            {
              kind: 'connections_incremental',
              interval_hours: 24,
              next_due: '2026-09-24T08:00:00Z',
              skipped_on_quick: true,
            },
          ],
        }),
    })
    const list = within(await screen.findByRole('list', { name: 'Next runs' }))
    const items = list.getAllByRole('listitem').map((item) => item.textContent)
    expect(items[0]).toMatch(/^Enrichment: every 6h, next due /)
    expect(items[1]).toBe('Incremental connections sync: not on quick checks')
  })

  it('lists every kind’s due time on Automatic with the weekly full sync', async () => {
    renderCard({
      'GET /api/v1/automation': () =>
        jsonResponse(automation({ linkedin: { position: 'automatic', depth: 'full' } })),
    })
    const list = within(await screen.findByRole('list', { name: 'Next runs' }))
    for (const item of list.getAllByRole('listitem')) {
      expect(item).toHaveTextContent(/every \d+h, next due /)
    }
  })

  it('says why the position couldn’t load, and lists no runs', async () => {
    renderCard({ 'GET /api/v1/automation': () => jsonResponse({ detail: 'boom' }, 500) })
    expect(await screen.findByRole('alert')).toHaveTextContent('boom')
    expect(screen.queryByRole('list', { name: 'Next runs' })).toBeNull()
  })
})
