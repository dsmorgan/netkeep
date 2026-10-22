import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ATTENTION_EVERY_MS } from '@/features/attention/api'
import { BATCH_EVERY_MS, PROGRESS_EVERY_MS } from '@/features/system-status/api'
import {
  gmailChip,
  linkedinChip,
  netkeepChip,
  part,
  serverStatus,
} from '@/features/system-status/test-support'

import { FakeEventSource, resetFakeEventSource } from './fake-event-source'
import { jsonResponse } from './fetch'
import { serve } from './install'
import { renderApp } from './render'

const CHECK_NOW = '/api/v1/poll-status/gmail-replies/check-now'
const STATUS = '/api/v1/system-status'

/** The LinkedIn chip stopped by a session flag, as the server rolls it up. */
function stopped(word: 'Logged out' | 'Checkpoint') {
  return serverStatus({
    linkedin: linkedinChip({
      tone: 'bad',
      word,
      fact: 'runs stopped',
      target: { to: '/linkedin', hash: 'session' },
    }),
  })
}

function bar() {
  return within(screen.getByRole('group', { name: 'System status' }))
}

/** The chip for `system`, once it reads `tone`. */
async function chip(system: 'gmail' | 'linkedin' | 'netkeep', tone?: string) {
  const found = () => {
    const node = document.querySelector<HTMLElement>(`header [data-system="${system}"]`)
    if (node === null) throw new Error(`no ${system} chip`)
    if (tone !== undefined) expect(node).toHaveAttribute('data-tone', tone)
    return node
  }
  return waitFor(found)
}

/** Opens the stream, so live updates read connected. */
function connect() {
  act(() => FakeEventSource.instances.at(-1)?.onopen?.())
}

beforeEach(() => {
  vi.stubGlobal('EventSource', FakeEventSource)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
  resetFakeEventSource()
})

describe('the top bar’s status chips (#751)', () => {
  it('shows Gmail, LinkedIn and netkeep, in that order, on every page', async () => {
    serve()
    await renderApp('/contacts')
    connect()
    await chip('linkedin', 'ok')
    await chip('netkeep', 'ok')

    const chips = bar().getAllByRole('button')
    expect(chips.map((node) => node.getAttribute('data-system'))).toEqual([
      'gmail',
      'linkedin',
      'netkeep',
    ])
    expect(chips[0]).toHaveTextContent(/^Gmail · Sending, checked .+ ago$/)
    expect(chips[1]).toHaveTextContent(/^LinkedIn · Armed, inbox .+ ago$/)
    expect(chips[2]).toHaveTextContent('netkeep · Connected, live')
    for (const node of chips) {
      expect(node).toHaveAttribute('aria-expanded', 'false')
      expect(node).toHaveAttribute('aria-haspopup', 'dialog')
    }
    // The old top-bar items are gone.
    expect(screen.queryByText(/Backend ok/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Background checks/ })).not.toBeInTheDocument()
  })

  it('gives each tone its own icon shape', async () => {
    serve({ [STATUS]: stopped('Logged out') })
    await renderApp('/contacts')
    const linkedin = await chip('linkedin', 'bad')
    expect(linkedin.querySelector('[data-icon="bad"]')).not.toBeNull()
    expect((await chip('gmail', 'ok')).querySelector('[data-icon="ok"]')).not.toBeNull()
    // Not live yet: a grey ring.
    expect((await chip('netkeep', 'off')).querySelector('[data-icon="off"]')).not.toBeNull()
  })

  it('keeps only the icon and name below sm, with the word and fact for screen readers', async () => {
    serve()
    await renderApp('/contacts')
    const gmail = await chip('gmail', 'ok')
    const word = within(gmail).getByText('· Sending')
    expect(word).toHaveClass('max-sm:sr-only')
    const fact = within(gmail).getByText(/^checked /)
    expect(fact).toHaveClass('max-sm:sr-only')
    // Full ink: an opacity would drop the fact under AA on the light tints (theme.test.ts).
    expect(fact.className).not.toMatch(/opacity/)
    expect(within(gmail).getByText('Gmail')).not.toHaveClass('max-sm:sr-only')
  })

  it('opens a popover of parts, closes on Escape and gives focus back to the chip', async () => {
    serve()
    await renderApp('/contacts')
    const linkedin = await chip('linkedin', 'ok')

    fireEvent.click(linkedin)
    const dialog = await screen.findByRole('dialog')
    expect(linkedin).toHaveAttribute('aria-expanded', 'true')
    const parts = within(dialog).getByRole('list', { name: 'LinkedIn status' })
    expect(
      within(parts)
        .getAllByRole('listitem')
        .map((row) => row.getAttribute('data-part')),
    ).toEqual(['session', 'schedule', 'inbox', 'prefills', 'heat'])
    expect(within(dialog).getByText(/^Updated /)).toBeInTheDocument()
    const open = within(dialog).getByRole('link', { name: 'Open LinkedIn' })
    expect(open).toHaveAttribute('href', '/linkedin#schedule')
    // All well: the button is not the primary one.
    expect(open).not.toHaveClass('bg-primary')

    fireEvent.keyDown(dialog, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    await waitFor(() => expect(linkedin).toHaveFocus())
    expect(linkedin).toHaveAttribute('aria-expanded', 'false')
  })

  it('makes the button primary, and the fix, when the chip needs you', async () => {
    serve({ [STATUS]: stopped('Checkpoint') })
    const { router } = await renderApp('/contacts')
    fireEvent.click(await chip('linkedin', 'bad'))
    const dialog = await screen.findByRole('dialog')
    const fix = within(dialog).getByRole('link', { name: 'Fix on the LinkedIn page' })
    expect(fix).toHaveAttribute('href', '/linkedin#session')
    expect(fix).toHaveClass('bg-primary')

    fireEvent.click(fix)
    await waitFor(() => expect(router.state.location.pathname).toBe('/linkedin'))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('keeps Check now in the Gmail popover', async () => {
    let posts = 0
    serve({
      [`POST ${CHECK_NOW}`]: () => {
        posts += 1
        return jsonResponse({ already_requested: false }, 202)
      },
    })
    await renderApp('/contacts')
    fireEvent.click(await chip('gmail', 'ok'))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('link', { name: 'Open Gmail' })).toHaveAttribute(
      'href',
      '/gmail',
    )

    fireEvent.click(within(dialog).getByRole('button', { name: 'Check now' }))
    await waitFor(() => expect(posts).toBe(1))
  })

  it('reads Unreachable in red, and Unknown in grey, while the server is down', async () => {
    // The default handler rejects every fetch (src/test/fetch.ts).
    await renderApp('/contacts')
    const app = await chip('netkeep', 'bad')
    expect(app).toHaveTextContent('netkeep · Unreachable, retrying')
    expect(await chip('gmail', 'off')).toHaveTextContent('Gmail · Unknown')
    expect(await chip('linkedin', 'off')).toHaveTextContent('LinkedIn · Unknown')

    fireEvent.click(app)
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Can’t reach netkeep · retrying every 15 s')).toBeVisible()
    // Settings can't load without the server: a reload, not a link.
    expect(within(dialog).queryByRole('link')).not.toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Reload' })).toBeInTheDocument()
  })

  it('reads the mailbox list while the Keychain is locked, never the mailbox status', async () => {
    const calls = serve({
      '/api/v1/mailboxes/status': () => jsonResponse({ detail: 'the Keychain is locked' }, 503),
    })
    await renderApp('/contacts')
    await chip('gmail', 'ok')
    expect(calls.some((call) => call.path === '/api/v1/mailboxes/status')).toBe(false)
  })

  it('changes the Gmail chip on the mailbox.status server event, without a reload', async () => {
    let current = serverStatus()
    serve({ [STATUS]: () => jsonResponse(current) })
    await renderApp('/contacts')
    await chip('gmail', 'ok')

    current = serverStatus({
      gmail: gmailChip({
        tone: 'bad',
        word: 'Re-authorize',
        fact: 'nothing sent or checked',
        target: { to: '/settings', hash: 'gmail' },
        page: 'Settings',
      }),
    })
    act(() => {
      FakeEventSource.instances.at(-1)?.emit('mailbox.status', {
        mailbox_id: 3,
        status: 'reauth_required',
        reason: 'invalid_grant',
      })
    })
    const gmail = await chip('gmail', 'bad')
    expect(gmail).toHaveTextContent('Gmail · Re-authorize, nothing sent or checked')
    fireEvent.click(gmail)
    expect(
      within(await screen.findByRole('dialog')).getByRole('link', {
        name: 'Fix on the Settings page',
      }),
    ).toHaveAttribute('href', '/settings#gmail')
  })

  it('refetches the roll-up after a change this tab makes', async () => {
    const calls = serve({ [`POST ${CHECK_NOW}`]: jsonResponse({ already_requested: false }, 202) })
    await renderApp('/contacts')
    fireEvent.click(await chip('gmail', 'ok'))
    const reads = () => calls.filter((call) => call.path === STATUS).length
    await waitFor(() => expect(reads()).toBeGreaterThan(0))
    const before = reads()
    fireEvent.click(
      within(await screen.findByRole('dialog')).getByRole('button', { name: 'Check now' }),
    )
    await waitFor(() => expect(reads()).toBeGreaterThan(before))
  })

  it('refetches LinkedIn when a run starts, spins while it runs, and moves its count', async () => {
    const started = new Date().toISOString()
    const running = (fact: string) =>
      serverStatus({
        linkedin: linkedinChip({
          tone: 'busy',
          word: 'Enrichment running',
          fact,
          target: { to: '/linkedin', hash: 'runs' },
          parts: [
            part('running', 'busy', 'Enrichment running', `Enrichment · ${fact}`, {
              label: 'Running',
              fact,
            }),
          ],
          times: { run_started: { at: started, style: 'ago' } },
        }),
      })
    let current = serverStatus()
    serve({ [STATUS]: () => jsonResponse(current) })
    await renderApp('/contacts')
    await chip('linkedin', 'ok')

    current = running('started {run_started}')
    act(() => FakeEventSource.instances.at(-1)?.emit('run.started', { run_id: 9 }))
    const linkedin = await chip('linkedin', 'busy')
    expect(linkedin.querySelector('[data-icon="busy"]')).toHaveClass(
      'animate-spin',
      'motion-reduce:animate-none',
    )
    await waitFor(() =>
      expect(linkedin).toHaveTextContent('LinkedIn · Enrichment running, started just now'),
    )

    current = running('12 of 50')
    act(() =>
      FakeEventSource.instances
        .at(-1)
        ?.emit('run.progress', { run_id: 9, planned: 50, visited: 12 }),
    )
    await waitFor(() =>
      expect(linkedin).toHaveTextContent('LinkedIn · Enrichment running, 12 of 50'),
    )
  })

  it('reads the roll-up at most once per window for a run’s progress events', async () => {
    const calls = serve()
    await renderApp('/contacts')
    await chip('linkedin', 'ok')
    const reads = () => calls.filter((call) => call.path === STATUS).length
    await waitFor(() => expect(reads()).toBeGreaterThan(0))
    vi.useFakeTimers({ shouldAdvanceTime: true })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PROGRESS_EVERY_MS)
    })
    const before = reads()

    act(() => {
      for (let visited = 1; visited <= 6; visited += 1) {
        FakeEventSource.instances.at(-1)?.emit('run.progress', { run_id: 9, visited, planned: 50 })
      }
    })
    await waitFor(() => expect(reads()).toBe(before + 1))
    // The last of them reads once more at the window's end, and only once.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PROGRESS_EVERY_MS + 1)
    })
    await waitFor(() => expect(reads()).toBe(before + 2))
  })

  it('refetches the session flag as often as /attention, so a flag set outside a run shows', async () => {
    let current = serverStatus()
    serve({ [STATUS]: () => jsonResponse(current) })
    vi.useFakeTimers({ shouldAdvanceTime: true })
    await renderApp('/contacts')
    await chip('linkedin', 'ok')

    // `netkeep preflight` found LinkedIn logged out; no event says so.
    current = stopped('Logged out')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ATTENTION_EVERY_MS + 1)
    })
    await chip('linkedin', 'bad')
  })

  it('spins Gmail for a campaign send batch, then clears it', async () => {
    let current = serverStatus()
    serve({ [STATUS]: () => jsonResponse(current) })
    await renderApp('/contacts')
    await chip('gmail', 'ok')

    vi.useFakeTimers({ shouldAdvanceTime: true })
    current = serverStatus({
      gmail: gmailChip({
        tone: 'busy',
        word: 'Sending now',
        fact: 'campaign batch',
        parts: [
          ...gmailChip().parts,
          part('batch', 'busy', 'Sending now', 'A campaign send batch just ran', {
            label: 'Campaign batch',
            fact: 'campaign batch',
          }),
        ],
      }),
    })
    act(() => FakeEventSource.instances.at(-1)?.emit('campaign.tick', { sent: 1 }))
    expect(await chip('gmail', 'busy')).toHaveTextContent('Gmail · Sending now, campaign batch')

    // The server shows the batch for 15 s; the chip reads again about then.
    current = serverStatus()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(BATCH_EVERY_MS + 1)
    })
    await chip('gmail', 'ok')
  })

  it('warns on the netkeep chip when serve isn’t running the background checks', async () => {
    serve({
      [STATUS]: serverStatus({
        netkeep: netkeepChip({
          parts: [
            part(
              'checks',
              'warn',
              'No background checks',
              'netkeep serve isn’t running, so nothing is checked in the background',
              { label: 'Background checks', fact: 'serve isn’t running' },
            ),
          ],
        }),
      }),
    })
    await renderApp('/contacts')
    connect()
    expect(await chip('netkeep', 'warn')).toHaveTextContent(
      'netkeep · No background checks, serve isn’t running',
    )
  })

  it('keeps every page up when an answer can’t be read, and says so', async () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => {})
    // A proxy page or an older backend: an answer with no chips.
    serve({ [STATUS]: {} })
    await renderApp('/contacts')
    expect(
      await screen.findByText('Status couldn’t be shown; trying again shortly'),
    ).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Contacts')
    error.mockRestore()
  })
})
