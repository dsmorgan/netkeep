import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { automation } from '@/features/automation/test-support'
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

async function radios() {
  return within(await screen.findByRole('radiogroup', { name: 'LinkedIn automation' }))
}

describe('ScheduleCard (#829)', () => {
  it('shows the LinkedIn switch, and no run times, on Manual', async () => {
    renderCard()
    const group = await radios()
    expect(group.getByRole('radio', { name: 'Manual' })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByText(/Manual: nothing runs on its own/)).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Next runs' })).toBeNull()
    // Pause is gone: Manual does its job.
    expect(screen.queryByRole('button', { name: /pause/i })).toBeNull()
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
    expect(
      screen.getByRole('checkbox', { name: 'Also run the weekly full sync and profile refreshes' }),
    ).toHaveAttribute('aria-checked', 'false')
  })

  it('asks before Automatic, with the profile-view notice, and sends confirm: true', async () => {
    const { calls } = renderCard({
      'PUT /api/v1/automation/linkedin': () =>
        jsonResponse(automation({ linkedin: { position: 'automatic' } })),
    })
    fireEvent.click((await radios()).getByRole('radio', { name: 'Automatic' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(await within(dialog).findByRole('note', { name: 'Profile views' })).toBeInTheDocument()
    expect(calls.some((call) => call.method === 'PUT')).toBe(false)

    fireEvent.click(within(dialog).getByRole('button', { name: 'Turn on Automatic' }))
    expect(await screen.findByRole('list', { name: 'Next runs' })).toBeInTheDocument()
    expect(calls.find((call) => call.method === 'PUT')?.body).toEqual({
      position: 'automatic',
      depth: 'full',
      confirm: true,
    })
  })

  it('asks before checking the weekly full sync box', async () => {
    const { calls } = renderCard({
      'GET /api/v1/automation': () =>
        jsonResponse(automation({ linkedin: { position: 'automatic', depth: 'quick' } })),
    })
    fireEvent.click(
      await screen.findByRole('checkbox', {
        name: 'Also run the weekly full sync and profile refreshes',
      }),
    )
    expect(
      await screen.findByRole('alertdialog', { name: 'Add the weekly full sync?' }),
    ).toBeInTheDocument()
    expect(calls.some((call) => call.method === 'PUT')).toBe(false)
  })

  it('turns Off in one click', async () => {
    const { calls } = renderCard({
      'PUT /api/v1/automation/linkedin': () =>
        jsonResponse(automation({ linkedin: { position: 'off' } })),
    })
    fireEvent.click((await radios()).getByRole('radio', { name: 'Off' }))
    expect(await screen.findByText(/Off: netkeep leaves LinkedIn alone/)).toBeInTheDocument()
    expect(screen.queryByRole('alertdialog')).toBeNull()
    await waitFor(() =>
      expect(calls.find((call) => call.method === 'PUT')?.body).toEqual({
        position: 'off',
        confirm: false,
      }),
    )
  })

  it('says why the switch couldn’t load, and shows no position', async () => {
    renderCard({ 'GET /api/v1/automation': () => jsonResponse({ detail: 'boom' }, 500) })
    expect(await screen.findByRole('alert')).toHaveTextContent('boom')
    expect(screen.queryByRole('radio')).toBeNull()
  })
})
