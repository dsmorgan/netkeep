import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ATTENTION_EVERY_MS } from '@/features/attention/api'
import { CAMPAIGN_SENDING_MS } from '@/features/attention/use-campaign-sending'
import { mailbox } from '@/features/mailboxes/test-support'
import { check, pollStatus, run } from '@/features/system-status/test-support'

import { FakeEventSource, resetFakeEventSource } from './fake-event-source'
import { jsonResponse } from './fetch'
import { linkedinStatus, serve } from './install'
import { renderApp } from './render'

const CHECK_NOW = '/api/v1/poll-status/gmail-replies/check-now'

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
    serve({ '/api/v1/linkedin/status': linkedinStatus({ session_flag: 'logged_out' }) })
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
    serve({ '/api/v1/linkedin/status': linkedinStatus({ session_flag: 'checkpoint' }) })
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
    let rows = [mailbox({ arm: 'send', armed_at: '2026-09-21T12:00:00Z' })]
    serve({ '/api/v1/mailboxes': () => jsonResponse(rows) })
    await renderApp('/contacts')
    await chip('gmail', 'ok')

    rows = [mailbox({ status: 'reauth_required', status_reason: 'invalid_grant' })]
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

  it('refetches LinkedIn when a run starts, spins while it runs, and moves its count', async () => {
    let current = linkedinStatus()
    let runs: unknown[] = []
    serve({
      '/api/v1/linkedin/status': () => jsonResponse(current),
      '/api/v1/linkedin/runs': () => jsonResponse({ items: runs, total: runs.length }),
    })
    await renderApp('/contacts')
    await chip('linkedin', 'ok')

    current = linkedinStatus({ running_run_id: 9 })
    runs = [run({ started_at: new Date().toISOString() })]
    act(() => FakeEventSource.instances.at(-1)?.emit('run.started', { run_id: 9 }))
    const linkedin = await chip('linkedin', 'busy')
    expect(linkedin.querySelector('[data-icon="busy"]')).toHaveClass(
      'animate-spin',
      'motion-reduce:animate-none',
    )
    await waitFor(() =>
      expect(linkedin).toHaveTextContent('LinkedIn · Enrichment running, started just now'),
    )

    act(() =>
      FakeEventSource.instances
        .at(-1)
        ?.emit('run.progress', { run_id: 9, planned: 50, visited: 12 }),
    )
    await waitFor(() =>
      expect(linkedin).toHaveTextContent('LinkedIn · Enrichment running, 12 of 50'),
    )
  })

  it('refetches the session flag as often as /attention, so a flag set outside a run shows', async () => {
    let current = linkedinStatus()
    serve({ '/api/v1/linkedin/status': () => jsonResponse(current) })
    vi.useFakeTimers({ shouldAdvanceTime: true })
    await renderApp('/contacts')
    await chip('linkedin', 'ok')

    // `netkeep preflight` found LinkedIn logged out; no event says so.
    current = linkedinStatus({ session_flag: 'logged_out' })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ATTENTION_EVERY_MS + 1)
    })
    await chip('linkedin', 'bad')
  })

  it('spins Gmail for a campaign send batch, then clears it', async () => {
    serve()
    await renderApp('/contacts')
    await chip('gmail', 'ok')

    vi.useFakeTimers({ shouldAdvanceTime: true })
    act(() => FakeEventSource.instances.at(-1)?.emit('campaign.tick', { sent: 1 }))
    expect(await chip('gmail', 'busy')).toHaveTextContent('Gmail · Sending now, campaign batch')

    act(() => {
      vi.advanceTimersByTime(CAMPAIGN_SENDING_MS + 1)
    })
    await chip('gmail', 'ok')
  })

  it('warns on the netkeep chip when serve isn’t running the background checks', async () => {
    serve({
      '/api/v1/poll-status': pollStatus({
        background_running: false,
        items: [check({ state: 'not_running', next_at: null })],
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
    // A proxy page or an older backend: a runs list with no items.
    serve({ '/api/v1/linkedin/runs': {} })
    await renderApp('/contacts')
    expect(
      await screen.findByText('Status couldn’t be shown; trying again shortly'),
    ).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Contacts')
    error.mockRestore()
  })
})
