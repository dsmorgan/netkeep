import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { Setup, SetupStep } from '@/features/dashboard/setup-api'
import { GRAPH } from '@/features/dashboard/steps-fixtures'

import { jsonResponse, mockFetch } from './fetch'
import { renderApp } from './render'

const STATES: Record<string, Partial<SetupStep>> = {
  about_you: { state: 'done', reason: 'Your details are set for test sends.' },
  import: { state: 'in_progress', next: true, reason: '1 draft import waiting to be finished.' },
  gmail: { state: 'skipped', skipped: true, reason: 'Skipped. Set up your Google Cloud project.' },
  chrome: { state: 'todo', next: true, reason: 'Start netkeep’s Chrome.' },
  first_sync: { state: 'blocked', reason: 'Waits for chrome.' },
}

function setupBody(complete = false, states = STATES): Setup {
  return {
    complete,
    steps: GRAPH.map((step) => ({
      ...step,
      title: `Step ${step.key}`,
      ...(states[step.key] ?? { state: 'blocked', reason: 'Waits for something.' }),
    })),
  }
}

/** Serves `/setup` (and its skip), and 404s the dashboard's other reads, which these tests don't read. */
function serve(setup: Setup | null) {
  const calls: { method: string; path: string }[] = []
  mockFetch((request) => {
    const url = new URL(request.url)
    calls.push({ method: request.method, path: url.pathname })
    if (url.pathname === '/api/v1/health')
      return jsonResponse({ status: 'ok', version: '0.0.1-test' })
    if (url.pathname === '/api/v1/setup' && setup !== null) return jsonResponse(setup)
    const skip = /^\/api\/v1\/setup\/steps\/(\w+)\/skip$/.exec(url.pathname)
    if (skip !== null && setup !== null) {
      const skipped = request.method === 'POST'
      return jsonResponse({
        ...setup,
        steps: setup.steps.map((step) =>
          step.key === skip[1]
            ? { ...step, skipped, state: skipped ? 'skipped' : 'todo', next: false }
            : step,
        ),
      })
    }
    return new Response('not found', { status: 404 })
  })
  return calls
}

function box(key: string): HTMLElement {
  const item = document.querySelector<HTMLElement>(`[data-step="${key}"]`)
  if (item === null) throw new Error(`no box for ${key}`)
  return item
}

describe('the setup map', () => {
  it('gives every box its state look, its reason and a link to its page', async () => {
    serve(setupBody(false))
    await renderApp('/')
    const list = await screen.findByRole('list', { name: 'Setup steps' })
    expect(within(list).getAllByRole('listitem')).toHaveLength(GRAPH.length)

    const look = (key: string) => box(key).querySelector('[data-slot="card"]')
    expect(look('about_you')).toHaveAttribute('data-look', 'done')
    expect(look('about_you')).toHaveAttribute('data-tone', 'ok')
    expect(look('import')).toHaveAttribute('data-look', 'next')
    expect(look('import')).toHaveAttribute('data-tone', 'accent')
    expect(look('chrome')).toHaveAttribute('data-look', 'next')
    expect(look('gmail')).toHaveAttribute('data-look', 'skipped')
    expect(look('first_sync')).toHaveAttribute('data-look', 'waiting')
    expect(look('first_sync')).not.toHaveAttribute('data-tone')

    expect(within(box('import')).getByText('In progress')).toBeInTheDocument()
    expect(within(box('chrome')).getByText('Next')).toBeInTheDocument()
    expect(within(box('first_sync')).getByText('Not ready')).toBeInTheDocument()
    expect(within(box('gmail')).getByText('Skipped')).toBeInTheDocument()
    expect(
      within(box('import')).getByText('1 draft import waiting to be finished.'),
    ).toBeInTheDocument()

    // A box that waits says so once, in its reason, not again as a "Needs" line.
    expect(within(box('first_sync')).queryByText(/^Needs/)).not.toBeInTheDocument()

    const href = (key: string) => within(box(key)).getByRole('link').getAttribute('href')
    expect(href('import')).toBe('/imports')
    expect(href('chrome')).toBe('/linkedin')
    expect(href('triage')).toBe('/triage')
    expect(href('build_list')).toBe('/lists')
    expect(href('campaign')).toBe('/campaigns')
    expect(href('about_you')).toBe('/settings')
  })

  it('reads in diagram order, prerequisites first, with the arrows hidden from screen readers', async () => {
    serve(setupBody(false))
    await renderApp('/')
    const list = await screen.findByRole('list', { name: 'Setup steps' })
    const order = within(list)
      .getAllByRole('listitem')
      .map((item) => item.getAttribute('data-step'))
    expect(order.indexOf('chrome')).toBeLessThan(order.indexOf('first_sync'))
    expect(order.indexOf('first_sync')).toBeLessThan(order.indexOf('arm_schedule'))
    expect(screen.getByTestId('steps-arrows')).toHaveAttribute('aria-hidden', 'true')
    // Every box is a heading a screen reader can move to.
    expect(within(list).getAllByRole('heading', { level: 3 })).toHaveLength(GRAPH.length)
  })

  it('skips and un-skips through the API, and offers no skip on a done step', async () => {
    const calls = serve(setupBody(false))
    await renderApp('/')
    await screen.findByRole('list', { name: 'Setup steps' })
    expect(within(box('about_you')).queryByRole('button')).not.toBeInTheDocument()

    fireEvent.click(within(box('chrome')).getByRole('button', { name: 'Skip Step chrome' }))
    await waitFor(() => expect(within(box('chrome')).getByText('Skipped')).toBeInTheDocument())
    expect(calls).toContainEqual({ method: 'POST', path: '/api/v1/setup/steps/chrome/skip' })

    fireEvent.click(within(box('gmail')).getByRole('button', { name: 'Un-skip Step gmail' }))
    await waitFor(() =>
      expect(calls).toContainEqual({ method: 'DELETE', path: '/api/v1/setup/steps/gmail/skip' }),
    )
  })

  it('starts nothing: every request it makes is a read of setup or a skip', async () => {
    const calls = serve(setupBody(false))
    await renderApp('/')
    await screen.findByRole('list', { name: 'Setup steps' })
    const writes = calls.filter((call) => call.method !== 'GET')
    expect(writes).toEqual([])
  })
})
