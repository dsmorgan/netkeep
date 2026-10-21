import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { Setup, SetupStep } from '@/features/dashboard/setup-api'
import { WIRES } from '@/features/dashboard/setup-map-layout'
import { GRAPH } from '@/features/dashboard/steps-fixtures'

import { jsonResponse, mockFetch } from './fetch'
import { renderApp } from './render'

/** Partway through: 1 done, 2 in progress, Gmail skipped, LinkedIn next, the rest waiting. */
const STATES: Record<string, Partial<SetupStep>> = {
  about_you: { state: 'done', reason: 'Your details are set for test sends.' },
  import: { state: 'in_progress', next: true, reason: '1 draft import waiting to be finished.' },
  gmail: { state: 'skipped', skipped: true, reason: 'Skipped. Set up your Google Cloud project.' },
  chrome: { state: 'todo', next: true, reason: 'Start netkeep’s Chrome.' },
  first_sync: { state: 'blocked', reason: 'Waits for 4.' },
  login_item: { state: 'todo', reason: 'Not turned on.' },
}

function setupBody(complete = false, states = STATES): Setup {
  return {
    complete,
    // Shuffled: the page orders the boxes by number, not by the answer's order.
    steps: [...GRAPH].reverse().map((step) => ({
      ...step,
      ...(states[step.key] ?? { state: 'blocked', reason: 'Waits for 2.' }),
    })),
  }
}

/**
 * Serves `/setup` and its skip, and 404s the dashboard's other reads, which these tests
 * don't read. `refuse` answers a skip with that 409 sentence instead.
 */
function serve(setup: Setup | null, refuse?: string) {
  const calls: { method: string; path: string }[] = []
  mockFetch((request) => {
    const url = new URL(request.url)
    calls.push({ method: request.method, path: url.pathname })
    if (url.pathname === '/api/v1/health')
      return jsonResponse({ status: 'ok', version: '0.0.1-test' })
    if (url.pathname === '/api/v1/setup' && setup !== null) return jsonResponse(setup)
    const skip = /^\/api\/v1\/setup\/steps\/(\w+)\/skip$/.exec(url.pathname)
    if (skip !== null && setup !== null) {
      if (refuse !== undefined) return jsonResponse({ detail: refuse }, 409)
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

const pane = (key: string) => box(key).querySelector('[data-look]')

describe('the setup map', () => {
  it('lists the boxes in number order, each with its number, state, reason and link', async () => {
    serve(setupBody(false))
    await renderApp('/')
    const list = await screen.findByRole('list', { name: 'Setup steps' })
    const items = within(list).getAllByRole('listitem')
    expect(items.map((item) => item.getAttribute('data-step'))).toEqual(
      GRAPH.map((step) => step.key),
    )
    // Each box says its number to a screen reader, and its title is a heading.
    expect(items[2]).toHaveTextContent(/^Step 3/)
    expect(within(list).getAllByRole('heading', { level: 3 })).toHaveLength(GRAPH.length)

    expect(pane('about_you')).toHaveAttribute('data-look', 'done')
    expect(pane('about_you')).toHaveAttribute('data-tone', 'ok')
    expect(pane('about_you')).toHaveClass('nk-tint', 'nk-tone-ok')
    expect(pane('import')).toHaveAttribute('data-look', 'next')
    expect(pane('import')).toHaveAttribute('data-tone', 'accent')
    expect(pane('chrome')).toHaveAttribute('data-look', 'next')
    expect(pane('gmail')).toHaveAttribute('data-look', 'skipped')
    expect(pane('first_sync')).toHaveAttribute('data-look', 'waiting')
    expect(pane('first_sync')).not.toHaveAttribute('data-tone')
    // Only the optional step has a dashed border.
    expect(pane('login_item')).toHaveAttribute('data-optional', 'true')
    expect(pane('triage')).not.toHaveAttribute('data-optional')

    expect(within(box('import')).getByText('In progress')).toBeInTheDocument()
    expect(within(box('chrome')).getByText('Next')).toBeInTheDocument()
    expect(within(box('first_sync')).getByText('Waiting')).toBeInTheDocument()
    expect(within(box('gmail')).getByText('Skipped')).toBeInTheDocument()
    expect(within(box('first_sync')).getByText('Waits for 4.')).toBeInTheDocument()

    // Each link opens its page at the step's section.
    const link = (key: string) => within(box(key)).getByRole('link')
    expect(link('about_you')).toHaveAccessibleName('Open About you')
    expect(link('about_you')).toHaveAttribute('href', '/settings#about-you')
    expect(link('import')).toHaveAccessibleName('Continue Import contacts')
    expect(link('import')).toHaveAttribute('href', '/imports')
    expect(link('chrome')).toHaveAccessibleName('Start Sign in to LinkedIn')
    expect(link('chrome')).toHaveAttribute('href', '/linkedin#session')
    expect(link('gmail')).toHaveAttribute('href', '/settings#gmail')
    expect(link('first_sync')).toHaveAttribute('href', '/linkedin#connections')
    expect(link('first_inbox')).toHaveAttribute('href', '/linkedin#inbox')
    expect(link('arm_schedule')).toHaveAttribute('href', '/linkedin#schedule')
    expect(link('triage')).toHaveAttribute('href', '/triage')
    expect(link('build_list').getAttribute('href')).toMatch(/^\/lists/)
    expect(link('campaign')).toHaveAttribute('href', '/campaigns')
    expect(link('login_item')).toHaveAttribute('href', '/settings#login-item')

    expect(screen.getByTestId('setup-progress')).toHaveTextContent(
      '2 of 10 required steps done or skipped · next: 2 Import contacts',
    )
  })

  it('draws the connectors in a hidden SVG, lit by state', async () => {
    serve(setupBody(false))
    await renderApp('/')
    await screen.findByRole('list', { name: 'Setup steps' })
    const svg = screen.getByTestId('setup-wires')
    expect(svg).toHaveAttribute('aria-hidden', 'true')
    const wire = (name: string) => svg.querySelector(`[data-wire="${name}"]`)
    expect(svg.querySelectorAll('path[data-wire]')).toHaveLength(WIRES.length)
    expect(wire('start-about_you')).toHaveAttribute('data-look', 'done')
    expect(wire('about_you-import')).toHaveAttribute('data-look', 'next')
    expect(wire('about_you-import')).toHaveClass('nk-wire-next')
    expect(wire('chrome-first_sync')).toHaveAttribute('data-look', 'base')
    expect(wire('gmail-triage')?.getAttribute('d')).toMatch(/^M/)
    // The groups and the end pills are drawing, not content.
    expect(document.querySelector('[data-group="Gmail"]')).toHaveAttribute('aria-hidden', 'true')
    expect(document.querySelector('[data-end="Ready"]')).toHaveAttribute('aria-hidden', 'true')
  })

  it('offers Skip and Undo skip only where the step can be skipped, through the API', async () => {
    const calls = serve(setupBody(false))
    await renderApp('/')
    await screen.findByRole('list', { name: 'Setup steps' })
    // 1 and 2 can't be skipped, and a waiting step has nothing to skip yet.
    expect(within(box('about_you')).queryByRole('button')).not.toBeInTheDocument()
    expect(within(box('import')).queryByRole('button')).not.toBeInTheDocument()
    expect(within(box('first_sync')).queryByRole('button')).not.toBeInTheDocument()

    fireEvent.click(within(box('chrome')).getByRole('button', { name: 'Skip Sign in to LinkedIn' }))
    await waitFor(() => expect(within(box('chrome')).getByText('Skipped')).toBeInTheDocument())
    expect(calls).toContainEqual({ method: 'POST', path: '/api/v1/setup/steps/chrome/skip' })

    fireEvent.click(
      within(box('gmail')).getByRole('button', { name: 'Undo skip of Connect Gmail' }),
    )
    await waitFor(() =>
      expect(calls).toContainEqual({ method: 'DELETE', path: '/api/v1/setup/steps/gmail/skip' }),
    )
  })

  it('offers nothing on a step skipped with 4, and only Undo skip on a waiting step skipped', async () => {
    serve(
      setupBody(false, {
        ...STATES,
        chrome: { state: 'skipped', skipped: true, reason: 'Skipped. Start netkeep’s Chrome.' },
        first_sync: { state: 'skipped', skipped: false, reason: 'Skipped with 4.' },
        campaign: { state: 'blocked', skipped: true, reason: 'Waits for 3 or 4.' },
      }),
    )
    await renderApp('/')
    await screen.findByRole('list', { name: 'Setup steps' })

    // Skipped with 4: it says so, and its skip is undone at 4, not here.
    expect(pane('first_sync')).toHaveAttribute('data-look', 'skipped')
    expect(within(box('first_sync')).getByText('Skipped')).toBeInTheDocument()
    expect(within(box('first_sync')).getByText('Skipped with 4.')).toBeInTheDocument()
    expect(within(box('first_sync')).queryByRole('button')).not.toBeInTheDocument()
    expect(
      within(box('chrome')).getByRole('button', { name: 'Undo skip of Sign in to LinkedIn' }),
    ).toBeInTheDocument()

    // Waiting and skipped: it still waits, and offers only to undo the skip.
    expect(pane('campaign')).toHaveAttribute('data-look', 'waiting')
    expect(within(box('campaign')).getByText('Waiting')).toBeInTheDocument()
    expect(within(box('campaign')).getAllByRole('button')).toHaveLength(1)
    expect(
      within(box('campaign')).getByRole('button', { name: 'Undo skip of Create a campaign' }),
    ).toBeInTheDocument()
  })

  it('keeps every box the same height: no state adds a border', async () => {
    serve(setupBody(false))
    await renderApp('/')
    await screen.findByRole('list', { name: 'Setup steps' })
    for (const key of ['about_you', 'import', 'gmail', 'first_sync', 'triage']) {
      expect(pane(key)?.className).not.toMatch(/(^|\s)border(-\S+)?(\s|$)/)
    }
  })

  it('shows step 11 without "(optional)", which its dashed border says, but still reads it', async () => {
    serve(setupBody(false))
    await renderApp('/')
    await screen.findByRole('list', { name: 'Setup steps' })
    const title = within(box('login_item')).getByRole('heading', { level: 3 })
    expect(title).toHaveAccessibleName('Run at login (optional)')
    expect(title.firstChild?.textContent).toBe('Run at login')
  })

  it('says why the server refused a skip', async () => {
    const refusal = 'A campaign needs Gmail or LinkedIn. Undo the other skip first.'
    serve(setupBody(false), refusal)
    await renderApp('/')
    await screen.findByRole('list', { name: 'Setup steps' })
    fireEvent.click(within(box('chrome')).getByRole('button', { name: 'Skip Sign in to LinkedIn' }))
    const region = screen.getByRole('region', { name: 'Setup' })
    expect(await within(region).findByRole('alert')).toHaveTextContent(refusal)
    expect(screen.queryByText(/failed \(409\)/)).not.toBeInTheDocument()
  })

  it('folds to one line once setup is complete, and shows the steps on request', async () => {
    const done = Object.fromEntries(GRAPH.map((step) => [step.key, { state: 'done' as const }]))
    serve(setupBody(true, done))
    await renderApp('/')
    const region = await screen.findByRole('region', { name: 'Setup is complete' })
    expect(within(region).queryByRole('list', { name: 'Setup steps' })).not.toBeInTheDocument()
    expect(screen.queryByTestId('setup-wires')).not.toBeInTheDocument()

    fireEvent.click(within(region).getByRole('button', { name: 'Show the steps' }))
    expect(within(region).getByRole('list', { name: 'Setup steps' })).toBeInTheDocument()
    expect(document.querySelector('[data-wire="login_item-ready"]')).toHaveAttribute(
      'data-look',
      'done',
    )

    fireEvent.click(within(region).getByRole('button', { name: 'Hide the steps' }))
    expect(within(region).queryByRole('list', { name: 'Setup steps' })).not.toBeInTheDocument()
  })

  it('lists steps it has no place for without the map', async () => {
    const setup = setupBody(false)
    serve({
      ...setup,
      steps: [...setup.steps, { ...GRAPH[0]!, key: 'newer_step', number: 12, title: 'Newer' }],
    })
    await renderApp('/')
    const list = await screen.findByRole('list', { name: 'Setup steps' })
    expect(within(list).getAllByRole('listitem')).toHaveLength(GRAPH.length + 1)
    expect(screen.queryByTestId('setup-wires')).not.toBeInTheDocument()
  })

  it('starts nothing: every request it makes is a read of setup or a skip', async () => {
    const calls = serve(setupBody(false))
    await renderApp('/')
    await screen.findByRole('list', { name: 'Setup steps' })
    const writes = calls.filter((call) => call.method !== 'GET')
    expect(writes).toEqual([])
  })
})
