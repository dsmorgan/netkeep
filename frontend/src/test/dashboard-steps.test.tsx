import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { Setup, SetupStep } from '@/features/dashboard/setup-api'
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
    // Shuffled: the page orders the rows by number, not by the answer's order.
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

function row(key: string): HTMLElement {
  const item = document.querySelector<HTMLElement>(`[data-step="${key}"]`)
  if (item === null) throw new Error(`no row for ${key}`)
  return item
}

function toggle(key: string): HTMLElement {
  const button = row(key).querySelector<HTMLElement>(':scope > button')
  if (button === null) throw new Error(`no toggle for ${key}`)
  return button
}

/** Opens a row, unless it is open already. */
function openRow(key: string) {
  if (toggle(key).getAttribute('aria-expanded') !== 'true') fireEvent.click(toggle(key))
}

const pane = () => screen.findByRole('region', { name: 'Set up netkeep' })
const groupButton = (name: RegExp) => screen.getByRole('button', { name })

describe('the setup checklist', () => {
  it('lists the rows in number order, each with its number, state and reason', async () => {
    serve(setupBody(false))
    await renderApp('/')
    const region = await pane()
    expect(within(region).getByText('Setup')).toHaveClass('nk-eyebrow')
    const rows = [...region.querySelectorAll('[data-step]')]
    expect(rows.map((item) => item.getAttribute('data-step'))).toEqual(
      GRAPH.map((step) => step.key),
    )
    // Each row says its number and its state to a screen reader.
    expect(toggle('gmail')).toHaveAccessibleName(/^Step 3: Connect Gmail, Skipped/)
    expect(toggle('first_sync')).toHaveAccessibleName(/^Step 5: First sync, Waiting/)
    expect(toggle('chrome')).toHaveAccessibleName(/^Step 4: Sign in to LinkedIn, Next/)
    expect(toggle('import')).toHaveAccessibleName(/^Step 2: Import contacts, In progress/)

    const mark = (key: string) => row(key).querySelector('[data-mark]')
    expect(mark('about_you')).toHaveAttribute('data-mark', 'done')
    expect(mark('about_you')?.querySelector('svg')).not.toBeNull()
    expect(mark('import')).toHaveAttribute('data-mark', 'next')
    expect(mark('gmail')).toHaveAttribute('data-mark', 'skipped')
    expect(mark('gmail')).toHaveClass('line-through')
    expect(mark('first_sync')).toHaveAttribute('data-mark', 'waiting')
    expect(mark('triage')).toHaveTextContent('8')

    expect(within(row('first_sync')).getByText('Waits for 4.')).toBeInTheDocument()
    expect(within(row('login_item')).getByText('Optional')).toBeInTheDocument()
    expect(toggle('login_item')).toHaveAccessibleName(/^Step 11: Run at login \(optional\), To do/)

    // N counts every step, the optional one too.
    expect(screen.getByTestId('setup-progress')).toHaveTextContent(
      '2 of 11 done · next: 2. Import contacts',
    )
  })

  it('opens the next step by default, and one row at a time', async () => {
    serve(setupBody(false))
    await renderApp('/')
    await pane()
    expect(toggle('import')).toHaveAttribute('aria-expanded', 'true')
    expect(document.querySelectorAll('[data-row-open]')).toHaveLength(1)
    expect(row('import')).toHaveAttribute('data-tone', 'accent')
    expect(row('import')).toHaveClass('nk-tint', 'nk-tone-accent', 'nk-flat')
    expect(
      within(row('import')).getByText(/Bring in a CSV or your LinkedIn data archive/),
    ).toBeInTheDocument()
    expect(within(row('import')).getByRole('link')).toHaveAccessibleName('Continue Import contacts')

    fireEvent.click(toggle('chrome'))
    expect(toggle('import')).toHaveAttribute('aria-expanded', 'false')
    expect(within(row('import')).queryByRole('link')).not.toBeInTheDocument()
    expect(toggle('chrome')).toHaveAttribute('aria-expanded', 'true')
    expect(within(row('chrome')).getByText(/Start netkeep’s own Chrome window/)).toBeInTheDocument()
    expect(document.querySelectorAll('[data-row-open]')).toHaveLength(1)

    // Clicking the open row closes it.
    fireEvent.click(toggle('chrome'))
    expect(document.querySelectorAll('[data-row-open]')).toHaveLength(0)
  })

  it('opens a done row as a filled green pane, with Open', async () => {
    serve(setupBody(false))
    await renderApp('/')
    await pane()
    openRow('about_you')
    expect(row('about_you')).toHaveAttribute('data-tone', 'ok')
    expect(row('about_you')).toHaveClass('nk-tint', 'nk-tone-ok')
    const link = within(row('about_you')).getByRole('link')
    expect(link).toHaveAccessibleName('Open About you')
    expect(link).toHaveAttribute('href', '/settings#about-you')
    // Nothing to skip on a done step.
    expect(within(row('about_you')).getAllByRole('button')).toHaveLength(1)
  })

  it('links each step to its page, at the step’s section', async () => {
    const todo = Object.fromEntries(GRAPH.map((step) => [step.key, { state: 'todo' as const }]))
    serve(setupBody(false, todo))
    await renderApp('/')
    await pane()
    const href = (key: string) => {
      openRow(key)
      return within(row(key)).getByRole('link').getAttribute('href')
    }
    expect(href('about_you')).toBe('/settings#about-you')
    expect(href('import')).toBe('/imports')
    expect(href('gmail')).toBe('/settings#gmail')
    expect(href('chrome')).toBe('/linkedin#session')
    expect(href('first_sync')).toBe('/linkedin#connections')
    expect(href('first_inbox')).toBe('/linkedin#inbox')
    expect(href('arm_schedule')).toBe('/linkedin#schedule')
    expect(href('triage')).toBe('/triage')
    expect(href('build_list')).toMatch(/^\/lists/)
    expect(href('campaign')).toBe('/campaigns')
    expect(href('login_item')).toBe('/settings#login-item')
    expect(within(row('login_item')).getByRole('link')).toHaveAccessibleName(
      'Start Run at login (optional)',
    )
  })

  it('keeps a waiting step’s Start, disabled', async () => {
    serve(setupBody(false))
    await renderApp('/')
    await pane()
    openRow('first_sync')
    const start = within(row('first_sync')).getByRole('button', { name: 'Start First sync' })
    expect(start).toBeDisabled()
    expect(within(row('first_sync')).queryByRole('link')).not.toBeInTheDocument()
  })

  it('offers Skip and Undo skip only where the step can be skipped, through the API', async () => {
    const calls = serve(setupBody(false))
    await renderApp('/')
    await pane()
    // 2 can't be skipped.
    expect(within(row('import')).getAllByRole('button')).toHaveLength(1)

    openRow('chrome')
    fireEvent.click(within(row('chrome')).getByRole('button', { name: 'Skip Sign in to LinkedIn' }))
    await waitFor(() =>
      expect(toggle('chrome')).toHaveAccessibleName(/Sign in to LinkedIn, Skipped/),
    )
    expect(calls).toContainEqual({ method: 'POST', path: '/api/v1/setup/steps/chrome/skip' })

    openRow('gmail')
    fireEvent.click(
      within(row('gmail')).getByRole('button', { name: 'Undo skip of Connect Gmail' }),
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
    await pane()

    // Skipped with 4: it says so, and its skip is undone at 4, not here.
    openRow('first_sync')
    expect(within(row('first_sync')).getByText('Skipped with 4.')).toBeInTheDocument()
    expect(
      within(row('first_sync')).queryByRole('button', { name: /^(Skip|Undo skip)/ }),
    ).toBeNull()
    openRow('chrome')
    expect(
      within(row('chrome')).getByRole('button', { name: 'Undo skip of Sign in to LinkedIn' }),
    ).toBeInTheDocument()

    // Waiting and skipped: it still waits, and offers only to undo the skip.
    openRow('campaign')
    expect(toggle('campaign')).toHaveAccessibleName(/Create a campaign, Waiting/)
    expect(within(row('campaign')).getByRole('button', { name: /Start/ })).toBeDisabled()
    expect(
      within(row('campaign')).getByRole('button', { name: 'Undo skip of Create a campaign' }),
    ).toBeInTheDocument()
  })

  it('says why the server refused a skip', async () => {
    const refusal = 'A campaign needs Gmail or LinkedIn. Undo the other skip first.'
    serve(setupBody(false), refusal)
    await renderApp('/')
    const region = await pane()
    openRow('chrome')
    fireEvent.click(within(row('chrome')).getByRole('button', { name: 'Skip Sign in to LinkedIn' }))
    expect(await within(region).findByRole('alert')).toHaveTextContent(refusal)
    expect(screen.queryByText(/failed \(409\)/)).not.toBeInTheDocument()
  })

  it('folds a finished group to "✓ Done", and each heading folds and unfolds', async () => {
    serve(
      setupBody(false, {
        ...STATES,
        import: { state: 'done', reason: '624 contacts.' },
      }),
    )
    await renderApp('/')
    await pane()
    const account = groupButton(/^Your account/)
    expect(account).toHaveAttribute('aria-expanded', 'false')
    expect(account).toHaveTextContent('✓ Done')
    expect(document.querySelector('[data-step="about_you"]')).toBeNull()

    fireEvent.click(account)
    expect(account).toHaveAttribute('aria-expanded', 'true')
    expect(row('about_you')).toBeInTheDocument()

    const reach = groupButton(/^Start reaching out/)
    expect(reach).toHaveAttribute('aria-expanded', 'true')
    expect(reach).toHaveTextContent('0 of 3')
    fireEvent.click(reach)
    expect(reach).toHaveAttribute('aria-expanded', 'false')
    expect(document.querySelector('[data-step="triage"]')).toBeNull()
  })

  it('shows Gmail and LinkedIn as two tracks under "Connect at least one"', async () => {
    serve(setupBody(false))
    await renderApp('/')
    await pane()
    const connect = groupButton(/^Connect at least one/)
    expect(connect).toHaveTextContent('None connected yet')
    expect(
      screen.getByText(/^Connect both to use everything netkeep does: Gmail sends email/),
    ).toHaveTextContent(/A campaign needs at least one\.$/)
    const gmail = document.querySelector<HTMLElement>('[data-track="Gmail"]')!
    const linkedin = document.querySelector<HTMLElement>('[data-track="LinkedIn"]')!
    expect(
      [...gmail.querySelectorAll<HTMLElement>('[data-step]')].map((li) => li.dataset.step),
    ).toEqual(['gmail'])
    expect(
      [...linkedin.querySelectorAll<HTMLElement>('[data-step]')].map((li) => li.dataset.step),
    ).toEqual(['chrome', 'first_sync', 'first_inbox', 'arm_schedule'])
  })

  it('says "✓ One connected" once a track is finished', async () => {
    serve(setupBody(false, { ...STATES, gmail: { state: 'done', reason: 'Sending as you.' } }))
    await renderApp('/')
    await pane()
    expect(groupButton(/^Connect at least one/)).toHaveTextContent('✓ One connected')
  })

  it('folds to one line once setup is complete, and shows the steps on request', async () => {
    const done = Object.fromEntries(GRAPH.map((step) => [step.key, { state: 'done' as const }]))
    serve(setupBody(true, done))
    await renderApp('/')
    const region = await screen.findByRole('region', { name: 'Setup is complete' })
    expect(region.querySelector('[data-group]')).toBeNull()

    fireEvent.click(within(region).getByRole('button', { name: 'Show the steps' }))
    expect(region.querySelectorAll('[data-group]')).toHaveLength(4)
    expect(screen.getByTestId('setup-progress')).toHaveTextContent(/^11 of 11 done$/)

    fireEvent.click(within(region).getByRole('button', { name: 'Hide the steps' }))
    expect(region.querySelector('[data-group]')).toBeNull()
  })

  it('still lists a step it has no group for', async () => {
    const setup = setupBody(false)
    serve({
      ...setup,
      steps: [...setup.steps, { ...GRAPH[0]!, key: 'newer_step', number: 12, title: 'Newer' }],
    })
    await renderApp('/')
    await pane()
    expect(row('newer_step')).toBeInTheDocument()
    expect(groupButton(/^More steps/)).toBeInTheDocument()
  })

  it('starts nothing: every request it makes is a read of setup', async () => {
    const calls = serve(setupBody(false))
    await renderApp('/')
    await pane()
    fireEvent.click(toggle('triage'))
    fireEvent.click(groupButton(/^Optional/))
    const writes = calls.filter((call) => call.method !== 'GET')
    expect(writes).toEqual([])
  })
})
