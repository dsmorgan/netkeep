import { act, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '@/api/client'
import { navStateText } from '@/components/layout/nav'
import { FakeEventSource, resetFakeEventSource } from '@/test/fake-event-source'
import { jsonResponse, mockFetch } from '@/test/fetch'
import { renderApp } from '@/test/render'

import {
  ATTENTION_EVENTS,
  attentionQuery,
  badgeText,
  changesAttention,
  documentTitle,
  needsYou,
  type Attention,
} from './api'

function attention(overrides: Partial<Attention> = {}): Attention {
  return {
    checked_at: '2026-10-14T15:00:00Z',
    counts: { inbox: 0, dashboard: 0, waiting: 0, try_again: 0, triage: 0, imports: 0 },
    flags: { dashboard: null, gmail: null, linkedin: null, settings: null },
    activity: {
      state: 'idle',
      label: 'Scheduled LinkedIn runs are off.',
      run_id: null,
      run_kind: null,
    },
    title_total: 0,
    ...overrides,
  }
}

const BUSY = attention({
  counts: { inbox: 3, dashboard: 120, waiting: 100, try_again: 20, triage: 4000, imports: 1 },
  flags: {
    dashboard: null,
    gmail: 'A mailbox needs you to sign in again.',
    linkedin: null,
    settings: null,
  },
  title_total: 124,
})

/** Serves `current()` at `/attention`, counts those fetches, and answers 204 to any change. */
function serve(current: () => Attention) {
  let fetches = 0
  mockFetch((request) => {
    const { pathname } = new URL(request.url)
    if (pathname === '/api/v1/attention') {
      fetches += 1
      return jsonResponse(current())
    }
    if (request.method !== 'GET') return new Response(null, { status: 204 })
    return jsonResponse({ detail: `unexpected ${pathname}` }, 404)
  })
  return () => fetches
}

function primaryNav() {
  return within(screen.getByRole('navigation', { name: 'Primary' }))
}

beforeEach(() => {
  vi.stubGlobal('EventSource', FakeEventSource)
})

afterEach(() => {
  vi.unstubAllGlobals()
  resetFakeEventSource()
  document.title = 'netkeeper'
})

describe('the attention helpers', () => {
  it('caps the pill at 99+', () => {
    expect(badgeText(1)).toBe('1')
    expect(badgeText(99)).toBe('99')
    expect(badgeText(100)).toBe('99+')
  })

  it('refreshes on a change, never on a read', () => {
    expect(changesAttention('PUT', '/api/v1/inbox/7/handled')).toBe(true)
    expect(changesAttention('POST', '/api/v1/campaigns/linkedin/messages/7/discard')).toBe(true)
    expect(changesAttention('POST', '/api/v1/campaigns/linkedin/prefill')).toBe(true)
    expect(changesAttention('POST', '/api/v1/triage/decisions')).toBe(true)
    expect(changesAttention('DELETE', 'http://127.0.0.1/api/v1/imports/4')).toBe(true)
    expect(changesAttention('GET', '/api/v1/inbox')).toBe(false)
    expect(changesAttention('POST', '/api/v1/contacts/query')).toBe(false)
    expect(changesAttention('POST', '/api/v1/autotag-rules/preview')).toBe(false)
    expect(changesAttention('POST', '/api/v1/templates/lint')).toBe(false)
  })

  it('titles the tab with every number but Triage', () => {
    expect(documentTitle(undefined)).toBe('netkeeper')
    expect(documentTitle(attention({ counts: { ...attention().counts, triage: 40 } }))).toBe(
      'netkeeper',
    )
    expect(documentTitle(BUSY)).toBe('(124) netkeeper')
  })

  it('marks the menu for a number or a dot, not for Triage alone', () => {
    expect(needsYou(undefined)).toBe(false)
    expect(needsYou(attention({ counts: { ...attention().counts, triage: 40 } }))).toBe(false)
    expect(needsYou(attention({ title_total: 1 }))).toBe(true)
    expect(
      needsYou(attention({ flags: { ...attention().flags, settings: 'A breaker stopped.' } })),
    ).toBe(true)
  })

  it('names the exact count and the flag', () => {
    expect(navStateText('inbox', 0, null)).toBe('')
    expect(navStateText('inbox', 3, null)).toBe(', 3 unhandled')
    expect(navStateText('dashboard', 120, null)).toBe(', 120 waiting for you')
    expect(navStateText('imports', 1, null)).toBe(', 1 draft to review')
    expect(navStateText('imports', 2, 'Held.')).toBe(', 2 drafts to review, Held.')
    expect(navStateText(undefined, 0, 'A mailbox needs you to sign in again.')).toBe(
      ', A mailbox needs you to sign in again.',
    )
  })

  it('polls every minute, in a background tab too', () => {
    expect(attentionQuery.refetchInterval).toBe(60_000)
    expect(attentionQuery.refetchIntervalInBackground).toBe(true)
  })
})

describe('the nav badges', () => {
  it('shows a pill per count and a dot per flag, with the state in each link’s name', async () => {
    serve(() => BUSY)
    await renderApp('/exports')

    const nav = primaryNav()
    const inbox = await nav.findByRole('link', { name: 'Inbox, 3 unhandled' })
    expect(within(inbox).getByTestId('attention-count')).toHaveTextContent('3')
    // The pill caps at 99+; the name keeps the exact count.
    const dashboard = nav.getByRole('link', { name: 'Dashboard, 120 waiting for you' })
    expect(within(dashboard).getByTestId('attention-count')).toHaveTextContent('99+')
    expect(nav.getByRole('link', { name: 'Triage, 4000 to triage' })).toBeInTheDocument()
    const gmail = nav.getByRole('link', { name: 'Gmail, A mailbox needs you to sign in again.' })
    expect(within(gmail).getByTestId('attention-dot')).toBeInTheDocument()
    expect(within(gmail).queryByTestId('attention-count')).toBeNull()
    // Nothing on the pages that show nothing in v1.
    expect(nav.getByRole('link', { name: 'Contacts' })).toBeInTheDocument()
    expect(nav.getByRole('link', { name: 'Exports' })).toBeInTheDocument()
    // Badges are hidden from assistive technology: the name carries them.
    for (const badge of screen.getAllByTestId('attention-count')) {
      expect(badge.closest('[aria-hidden="true"]')).not.toBeNull()
    }
  })

  it('sends the Imports badge to the import history, where drafts are listed', async () => {
    serve(() => BUSY)
    await renderApp('/imports')

    const imports = await primaryNav().findByRole('link', { name: 'Imports, 1 draft to review' })
    expect(imports).toHaveAttribute('href', '/imports/runs')
    // Still the current section on the upload page.
    expect(imports).toHaveAttribute('data-status', 'active')
    expect(imports).toHaveAttribute('aria-current', 'page')
  })

  it('keeps the Imports link on the upload page while no draft waits', async () => {
    serve(() => attention())
    await renderApp('/exports')

    await waitFor(() =>
      expect(primaryNav().getByRole('link', { name: 'Imports' })).toHaveAttribute(
        'href',
        '/imports',
      ),
    )
  })

  it('shows no badges, and a plain title, while the count can’t be read', async () => {
    mockFetch(() => jsonResponse({ detail: 'down' }, 500))
    await renderApp('/exports')

    expect(primaryNav().getByRole('link', { name: 'Inbox' })).toBeInTheDocument()
    expect(screen.queryByTestId('attention-count')).toBeNull()
    expect(document.title).toBe('netkeeper')
  })
})

describe('the menu dot and the tab title', () => {
  it('marks the menu button, with a label, and counts in the tab title', async () => {
    serve(() => BUSY)
    await renderApp('/exports')

    const menu = await screen.findByRole('button', {
      name: 'Open navigation, something needs you',
    })
    expect(within(menu).getByTestId('attention-dot')).toBeInTheDocument()
    await waitFor(() => expect(document.title).toBe('(124) netkeeper'))
  })

  it('leaves the menu plain when only Triage has a backlog', async () => {
    serve(() => attention({ counts: { ...attention().counts, triage: 4000 } }))
    await renderApp('/exports')

    await primaryNav().findByRole('link', { name: 'Triage, 4000 to triage' })
    expect(screen.getByRole('button', { name: 'Open navigation' })).toBeInTheDocument()
    expect(document.title).toBe('netkeeper')
  })
})

describe('refreshing', () => {
  it.each(ATTENTION_EVENTS)('refetches at once on %s', async (type) => {
    let current = attention()
    const fetches = serve(() => current)
    await renderApp('/exports')
    await waitFor(() => expect(fetches()).toBe(1))

    current = attention({ counts: { ...attention().counts, inbox: 2 }, title_total: 2 })
    const source = FakeEventSource.instances.at(-1)
    expect(source).toBeDefined()
    act(() => source?.emit(type, {}))

    expect(await primaryNav().findByRole('link', { name: 'Inbox, 2 unhandled' })).toBeVisible()
    expect(fetches()).toBe(2)
  })

  it('refetches after a change this tab makes, and not after a read', async () => {
    let current = attention({ counts: { ...attention().counts, inbox: 1 }, title_total: 1 })
    const fetches = serve(() => current)
    await renderApp('/exports')
    await primaryNav().findByRole('link', { name: 'Inbox, 1 unhandled' })
    const before = fetches()

    await api.GET('/api/v1/inbox')
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(fetches()).toBe(before)

    current = attention()
    await api.PUT('/api/v1/inbox/{message_id}/handled', {
      params: { path: { message_id: 7 } },
      body: { handled: true },
    })

    expect(await primaryNav().findByRole('link', { name: 'Inbox' })).toBeInTheDocument()
    expect(fetches()).toBe(before + 1)
    await waitFor(() => expect(document.title).toBe('netkeeper'))
  })
})
