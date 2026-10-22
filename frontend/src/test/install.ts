/**
 * A quiet, healthy install for app-level tests of the dashboard and the top bar's
 * status chips: every read they make, answered (invented data only).
 */
import { GRAPH } from '@/features/dashboard/steps-fixtures'
import { mailbox } from '@/features/mailboxes/test-support'
import { pollStatus, serverStatus } from '@/features/system-status/test-support'

import { jsonResponse, mockFetch } from './fetch'

export function attention(overrides: Record<string, unknown> = {}) {
  return {
    checked_at: '2026-10-20T12:00:00Z',
    counts: { inbox: 0, dashboard: 0, waiting: 0, try_again: 0, triage: 0, imports: 0 },
    flags: { dashboard: null, gmail: null, linkedin: null, settings: null },
    activity: { state: 'idle', label: '', run_id: null, run_kind: null },
    title_total: 0,
    ...overrides,
  }
}

export function linkedinStatus(overrides: Record<string, unknown> = {}) {
  return {
    session_flag: null,
    session_flagged_at: null,
    heat_tripped: false,
    armed: true,
    schedule_paused: false,
    running_run_id: null,
    can_start_runs: true,
    ...overrides,
  }
}

/** Every read the dashboard makes, answered with a quiet, healthy install. */
export function quiet(): Record<string, unknown> {
  return {
    '/api/v1/health': { status: 'ok', version: '0.0.1-test' },
    '/api/v1/poll-status': pollStatus(),
    '/api/v1/system-status': serverStatus(),
    '/api/v1/setup': {
      complete: false,
      steps: GRAPH.map((step) => ({ ...step, title: `Step ${step.key}`, reason: 'Waits.' })),
    },
    '/api/v1/attention': attention(),
    '/api/v1/mailboxes': [mailbox({ arm: 'send', armed_at: '2026-09-21T12:00:00Z' })],
    '/api/v1/linkedin/status': linkedinStatus(),
    '/api/v1/linkedin/runs': { items: [], total: 0 },
    '/api/v1/linkedin/schedule': {
      armed: true,
      armed_at: '2026-09-20T12:00:00Z',
      paused: false,
      paused_at: null,
      scheduler_running: true,
      jobs: [],
    },
    '/api/v1/linkedin/budget': {
      budgets: [{ action: 'li_prefills', day: { count: 13, limit: 15, remaining: 2 }, week: null }],
      profile_visits_today: {
        ramp: 10,
        after_weekend: 10,
        after_heat: 10,
        spent_today: 0,
        week_left: null,
        remaining: 10,
      },
      profile_view_notice: 'Enrichment can show up in Who viewed your profile.',
      risk_warning: null,
    },
    '/api/v1/linkedin/heat': {
      score: 0.5,
      threshold: 5,
      multiplier: 1,
      tripped: false,
      last_raised_at: null,
      cleared_at: null,
      resumes_at: null,
    },
    '/api/v1/dashboard/metrics': {
      window: '7d',
      start: '2026-10-14',
      end: '2026-10-20',
      sent: 0,
      replies: 0,
      reply_rate: null,
      bounced: 0,
      changed_jobs: 0,
      new_contacts: { total: 0, linkedin: 0, imported: 0 },
      series: [],
    },
    '/api/v1/dashboard/next-fires': { items: [], total: 0 },
    '/api/v1/dashboard/changed-jobs': { items: [], total: 0, days: 30 },
    '/api/v1/dashboard/inbound': {
      count: 0,
      since: '2026-10-13T12:00:00Z',
      reply_detection: false,
    },
    '/api/v1/campaigns': [],
    '/api/v1/campaigns/linkedin/ready': { items: [], total: 0, try_again: [] },
    '/api/v1/campaigns/linkedin/waiting': { items: [], total: 0 },
    '/api/v1/campaigns/linkedin/options': { auto_send: false },
  }
}

export type Answer = unknown | ((request: Request) => Response | Promise<Response>)

/** Serves `quiet()` with `overrides` on top; anything else answers 404. */
export function serve(overrides: Record<string, Answer> = {}) {
  const calls: { method: string; path: string; body: string }[] = []
  const routes: Record<string, Answer> = { ...quiet(), ...overrides }
  mockFetch(async (request) => {
    const { pathname } = new URL(request.url)
    const body = request.method === 'GET' ? '' : await request.clone().text()
    calls.push({ method: request.method, path: pathname, body })
    const answer = routes[`${request.method} ${pathname}`] ?? routes[pathname]
    if (answer === undefined) return new Response('not found', { status: 404 })
    return typeof answer === 'function'
      ? (answer as (request: Request) => Response | Promise<Response>)(request)
      : jsonResponse(answer)
  })
  return calls
}
