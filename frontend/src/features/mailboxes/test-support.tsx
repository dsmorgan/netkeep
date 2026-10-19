import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render } from '@testing-library/react'
import type { ReactNode } from 'react'

import { EventStreamContext } from '@/features/events/event-stream-context'
import { FakeEventSource } from '@/test/fake-event-source'
import { jsonResponse, mockFetch } from '@/test/fetch'

import type { Mailbox, MailboxArmSteps, MailboxStatus } from './api'

export interface Call {
  method: string
  path: string
  body: unknown
}

export function mailbox(overrides: Partial<Mailbox> = {}): Mailbox {
  return {
    id: 3,
    email: 'sender@example.com',
    provider: 'gmail',
    status: 'ok',
    status_reason: null,
    daily_cap: 80,
    label_prefix: 'netkeep',
    checked_at: '2026-09-26T12:00:00Z',
    arm: null,
    armed_at: null,
    send_armed_at: null,
    armed_by: null,
    message_id_verified_at: null,
    message_id_checked_at: null,
    created_at: '2026-09-20T12:00:00Z',
    updated_at: '2026-09-26T12:00:00Z',
    ...overrides,
  }
}

export function status(overrides: Partial<MailboxStatus> = {}): MailboxStatus {
  const mailboxes = overrides.mailboxes ?? []
  return {
    client_configured: true,
    client_id: '1234-fake.apps.googleusercontent.com',
    keychain_service: 'netkeep',
    mailboxes,
    reauth_required: mailboxes.some((row) => row.status === 'reauth_required'),
    arm_steps: [],
    ...overrides,
  }
}

/** A mailbox's way to arming to send (#510): armed for drafts, nothing found yet. */
export function armSteps(overrides: Partial<MailboxArmSteps> = {}): MailboxArmSteps {
  return {
    mailbox_id: 3,
    armed_for_drafts: true,
    armed_to_send: false,
    verified_at: null,
    waiting: 'no_draft',
    checked_at: null,
    check_requested_at: null,
    checks_running: true,
    can_arm_send: false,
    next_step: 'send_test',
    test_campaign_id: null,
    ...overrides,
  }
}

/** The guided setup's progress (#302) before anything is stored. */
export function emptySetup(overrides: Record<string, unknown> = {}) {
  return {
    project_id: null,
    sender_email: null,
    done: [],
    steps: ['project', 'gmail_api', 'branding', 'test_user', 'client_created', 'published'],
    ...overrides,
  }
}

type Handler = (body: unknown) => Response

/**
 * A fake backend: `routes` keyed `"METHOD /path"`, the status and list routes
 * reading `current()` on every call, so a test can change what the next fetch sees.
 * `GET /gmail-setup` answers an empty setup unless a route says otherwise.
 */
export function renderWithBackend(
  ui: ReactNode,
  current: () => MailboxStatus,
  routes: Record<string, Handler> = {},
) {
  const calls: Call[] = []
  mockFetch(async (request) => {
    const { pathname } = new URL(request.url)
    const text = request.method === 'GET' ? '' : await request.text()
    const body: unknown = text === '' ? null : JSON.parse(text)
    calls.push({ method: request.method, path: pathname, body })
    const route = routes[`${request.method} ${pathname}`]
    if (route !== undefined) return route(body)
    if (pathname === '/api/v1/mailboxes/status') return jsonResponse(current())
    if (pathname === '/api/v1/mailboxes') return jsonResponse(current().mailboxes)
    if (request.method === 'GET' && pathname === '/api/v1/gmail-setup') {
      return jsonResponse(emptySetup())
    }
    return jsonResponse({ detail: `unexpected ${request.method} ${pathname}` }, 500)
  })
  const source = new FakeEventSource('/api/v1/events')
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <EventStreamContext value={{ status: 'connected', source: source as unknown as EventSource }}>
        {ui}
      </EventStreamContext>
    </QueryClientProvider>,
  )
  return { ...utils, calls, source, queryClient }
}
