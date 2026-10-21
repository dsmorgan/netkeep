import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import type { DashboardMetrics, MetricsWindow } from '@/features/dashboard/api'
import { WINDOW_KEY } from '@/features/dashboard/metrics'

import { jsonResponse, mockFetch } from './fetch'
import { renderApp } from './render'

const BUCKETS = { sent: 0, replies: 0, bounced: 0, changed_jobs: 0, new_contacts: 0 }

/** Invented numbers for each window. */
const METRICS: Record<MetricsWindow, DashboardMetrics> = {
  '7d': {
    window: '7d',
    start: '2026-10-14',
    end: '2026-10-20',
    sent: 18,
    replies: 4,
    reply_rate: 4 / 18,
    bounced: 1,
    changed_jobs: 3,
    new_contacts: { total: 9, linkedin: 7, imported: 2 },
    series: [3, 0, 5, 2, 4, 1, 3].map((sent, index) => ({
      ...BUCKETS,
      start: `2026-10-${14 + index}`,
      sent,
    })),
  },
  '30d': {
    window: '30d',
    start: '2026-09-21',
    end: '2026-10-20',
    sent: 71,
    replies: 15,
    reply_rate: 15 / 71,
    bounced: 3,
    changed_jobs: 8,
    new_contacts: { total: 41, linkedin: 35, imported: 6 },
    series: [],
  },
  '1y': {
    window: '1y',
    start: '2025-11-01',
    end: '2026-10-20',
    sent: 412,
    replies: 88,
    reply_rate: 88 / 412,
    bounced: 14,
    changed_jobs: 37,
    new_contacts: { total: 640, linkedin: 212, imported: 428 },
    series: [{ ...BUCKETS, start: '2026-10-01', sent: 71 }],
  },
}

type Answer = (period: MetricsWindow) => Response | Promise<Response>

/** Serves the metrics (by `answer`), and 404s the dashboard's other reads. */
function serve(answer: Answer = (period) => jsonResponse(METRICS[period])) {
  const asked: string[] = []
  mockFetch((request) => {
    const url = new URL(request.url)
    if (url.pathname === '/api/v1/health')
      return jsonResponse({ status: 'ok', version: '0.0.1-test' })
    if (url.pathname === '/api/v1/dashboard/metrics') {
      const period = (url.searchParams.get('window') ?? '7d') as MetricsWindow
      asked.push(period)
      return answer(period)
    }
    return new Response('not found', { status: 404 })
  })
  return asked
}

const region = () =>
  screen.getByRole('main').querySelector('[data-region="metrics"]') as HTMLElement
const tile = (key: string) => region().querySelector(`[data-tile="${key}"]`) as HTMLElement

afterEach(() => {
  window.localStorage.clear()
})

describe('the metric tiles', () => {
  it('shows the five numbers for the last 7 days, each with a sparkline', async () => {
    const asked = serve()
    await renderApp('/')
    const tiles = await within(region()).findByRole('list', { name: 'Metrics' })
    expect(within(region()).getByRole('heading', { level: 2 })).toHaveTextContent('Last 7 days')
    expect(asked).toEqual(['7d'])
    expect(
      within(tiles)
        .getAllByRole('listitem')
        .map((item) => item.dataset.tile),
    ).toEqual(['sent', 'replies', 'bounced', 'changed_jobs', 'new_contacts'])
    expect(tile('sent')).toHaveTextContent('Sent18')
    expect(tile('replies')).toHaveTextContent('Replies4 22% reply rate')
    expect(tile('bounced')).toHaveTextContent('Bounced1')
    expect(tile('changed_jobs')).toHaveTextContent('Changed jobs3')
    expect(tile('new_contacts')).toHaveTextContent('New contacts9')
    expect(tile('new_contacts')).toHaveTextContent('7 LinkedIn · 2 imported')
    expect(tile('sent')).toHaveTextContent('Oct 14–20')
    // The sparkline is drawing, hidden from screen readers, with its line and its endpoint.
    const spark = within(tile('sent')).getByTestId('sparkline')
    expect(spark).toHaveAttribute('aria-hidden', 'true')
    expect(spark.querySelector('polyline')).toHaveAttribute(
      'points',
      '0,13.8 25,27 50,5 75,18.2 100,9.4 125,22.6 150,13.8',
    )
    expect(spark.querySelector('polygon')).not.toBeNull()
    expect(within(region()).queryByText(/Nothing yet/)).not.toBeInTheDocument()
  })

  it('switches the window, says which in the heading, and remembers it', async () => {
    const asked = serve()
    const { unmount } = await renderApp('/')
    await within(region()).findByRole('list', { name: 'Metrics' })
    const year = within(region()).getByRole('button', { name: '1 year' })
    expect(year).toHaveAttribute('aria-pressed', 'false')

    fireEvent.click(year)
    expect(year).toHaveAttribute('aria-pressed', 'true')
    expect(within(region()).getByRole('heading', { level: 2 })).toHaveTextContent('Last 12 months')
    await waitFor(() => expect(tile('sent')).toHaveTextContent('Sent412'))
    expect(tile('sent')).toHaveTextContent('Nov 2025–Oct 2026')
    expect(asked).toEqual(['7d', '1y'])
    expect(window.localStorage.getItem(WINDOW_KEY)).toBe('1y')

    fireEvent.click(within(region()).getByRole('button', { name: '30 days' }))
    expect(within(region()).getByRole('heading', { level: 2 })).toHaveTextContent('Last 30 days')
    await waitFor(() => expect(tile('sent')).toHaveTextContent('Sent71'))

    // A fresh visit opens on the window picked last.
    unmount()
    asked.length = 0
    await renderApp('/')
    await within(region()).findByRole('list', { name: 'Metrics' })
    expect(asked).toEqual(['30d'])
  })

  it('says it is counting while the numbers load', async () => {
    let answer: (response: Response) => void = () => {}
    serve(() => new Promise<Response>((resolve) => (answer = resolve)))
    await renderApp('/')
    expect(await within(region()).findByRole('status')).toHaveTextContent('Counting…')
    answer(jsonResponse(METRICS['7d']))
    await within(region()).findByRole('list', { name: 'Metrics' })
  })

  it('says so when nothing happened in the window, and still shows the zeros', async () => {
    serve((period) =>
      jsonResponse({
        ...METRICS[period],
        sent: 0,
        replies: 0,
        reply_rate: null,
        bounced: 0,
        changed_jobs: 0,
        new_contacts: { total: 0, linkedin: 0, imported: 0 },
        series: [],
      }),
    )
    await renderApp('/')
    expect(await within(region()).findByText(/^Nothing yet in this window/)).toBeInTheDocument()
    expect(tile('sent')).toHaveTextContent('Sent0')
    // No reply rate without a send.
    expect(tile('replies')).not.toHaveTextContent('%')
  })

  it('says the numbers couldn’t be read, and the rest of the page still renders', async () => {
    serve(() => jsonResponse({ detail: 'boom' }, 500))
    await renderApp('/')
    expect(await within(region()).findByRole('alert')).toHaveTextContent(
      'The numbers couldn’t be read',
    )
    expect(screen.getByRole('region', { name: 'Needs you' })).toBeInTheDocument()
  })
})
