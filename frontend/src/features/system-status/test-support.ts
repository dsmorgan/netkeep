/** Fixtures for the status roll-up and the problems (invented data only). */
import type { Attention } from '@/features/attention/api'
import type { BudgetStatus, Heat, LinkedInStatus, Run, Schedule } from '@/features/linkedin/types'
import type { PollCheck, PollStatus } from '@/features/poll-status/api'

import type { Read, ServerChip, ServerStatus } from './roll-up'

export const ok = <T>(data: T): Read<T> => ({ state: 'ok', data })

export function run(overrides: Partial<Run> = {}): Run {
  return {
    id: 9,
    kind: 'enrich',
    trigger: 'manual',
    status: 'running',
    started_at: '2026-10-21T11:57:00Z',
    planned: null,
    completed: null,
    progress: null,
    ...overrides,
  } as Run
}

export function check(overrides: Partial<PollCheck> = {}): PollCheck {
  return {
    key: 'gmail_replies',
    group: 'gmail',
    label: 'Gmail replies',
    state: 'scheduled',
    interval_minutes: 10,
    last_at: '2026-10-21T11:58:00Z',
    next_at: '2026-10-21T12:08:00Z',
    reason: null,
    requested: false,
    requested_at: null,
    ...overrides,
  }
}

export function pollStatus(overrides: Partial<PollStatus> = {}): PollStatus {
  return {
    checked_at: '2026-10-21T12:00:00Z',
    background_running: true,
    items: [
      check(),
      check({
        key: 'linkedin_inbox',
        group: 'linkedin',
        label: 'LinkedIn inbox',
        interval_minutes: 60,
        last_at: '2026-10-21T11:26:00Z',
        next_at: '2026-10-21T12:26:00Z',
      }),
    ],
    mailboxes: [],
    ...overrides,
  }
}

export function linkedinStatus(overrides: Partial<LinkedInStatus> = {}): LinkedInStatus {
  return {
    session_flag: null,
    session_flagged_at: null,
    heat_tripped: false,
    armed: true,
    schedule_paused: false,
    running_run_id: null,
    can_start_runs: true,
    ...overrides,
  } as LinkedInStatus
}

export function schedule(overrides: Partial<Schedule> = {}): Schedule {
  return {
    armed: true,
    armed_at: '2026-09-20T12:00:00Z',
    paused: false,
    paused_at: null,
    scheduler_running: true,
    jobs: [],
    ...overrides,
  } as Schedule
}

export function budget(
  prefills: { count: number; limit: number; remaining: number } | null,
): BudgetStatus {
  return {
    budgets: prefills === null ? [] : [{ action: 'li_prefills', day: prefills, week: null }],
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
  } as BudgetStatus
}

export function heat(tripped = false): Heat {
  return {
    score: tripped ? 6 : 0.5,
    threshold: 5,
    multiplier: 1,
    tripped,
    last_raised_at: null,
    cleared_at: null,
    resumes_at: null,
  } as Heat
}

export function attention(overrides: Partial<Attention> = {}): Attention {
  return {
    checked_at: '2026-10-20T12:00:00Z',
    counts: { inbox: 0, dashboard: 0, waiting: 0, try_again: 0, triage: 0, imports: 0 },
    flags: { dashboard: null, gmail: null, linkedin: null, settings: null },
    activity: { state: 'idle', label: '', run_id: null, run_kind: null },
    title_total: 0,
    ...overrides,
  } as Attention
}

// --- `GET /system-status` (#761), as the server answers it ------------------------------

type ServerPart = ServerChip['parts'][number]

/** One server part; `fact` and `target` null unless given. */
export function part(
  key: string,
  tone: ServerPart['tone'],
  word: string,
  detail: string,
  overrides: Partial<ServerPart> = {},
): ServerPart {
  return { key, label: key, tone, word, detail, fact: null, target: null, ...overrides }
}

/** The Gmail chip of a healthy install: armed to send, replies checked 2 min before noon. */
export function gmailChip(overrides: Partial<ServerChip> = {}): ServerChip {
  return {
    system: 'gmail',
    name: 'Gmail',
    tone: 'ok',
    word: 'Sending',
    fact: 'checked {replies_last}',
    parts: [
      part('mailbox', 'ok', 'Connected', 'sender@example.com · connected', { label: 'Mailbox' }),
      part('sending', 'ok', 'Sending', 'Armed to send', { label: 'Sending' }),
      part('replies', 'ok', 'Checked', 'Checked {replies_last} · next {replies_next}', {
        label: 'Reply check',
        fact: 'checked {replies_last}',
      }),
    ],
    target: { to: '/gmail', hash: null },
    page: 'Gmail',
    times: {
      replies_last: { at: '2026-10-21T11:58:00Z', style: 'ago' },
      replies_next: { at: '2026-10-21T12:08:00Z', style: 'next' },
    },
    ...overrides,
  }
}

const SCHEDULE = { to: '/linkedin' as const, hash: 'schedule' }

/** The LinkedIn chip of a healthy install: armed, inbox checked 34 min before noon. */
export function linkedinChip(overrides: Partial<ServerChip> = {}): ServerChip {
  return {
    system: 'linkedin',
    name: 'LinkedIn',
    tone: 'ok',
    word: 'Armed',
    fact: 'inbox {inbox_last}',
    parts: [
      part(
        'session',
        'ok',
        'No session flag',
        'No session flag: no run has found LinkedIn logged out',
        {
          label: 'Session',
          target: { to: '/linkedin', hash: 'session' },
        },
      ),
      part('schedule', 'ok', 'Armed', 'Armed', { label: 'Scheduled runs', target: SCHEDULE }),
      part('inbox', 'ok', 'Checked', 'Checked {inbox_last} · next {inbox_next}', {
        label: 'Inbox check',
        fact: 'inbox {inbox_last}',
        target: { to: '/linkedin', hash: 'inbox' },
      }),
      part('prefills', 'ok', '2 prefills left', '2 of 15 left', {
        label: 'Prefills today',
        target: { to: '/linkedin', hash: 'budget' },
      }),
      part('heat', 'ok', 'Heat normal', 'Normal', {
        label: 'Heat',
        target: { to: '/linkedin', hash: 'heat' },
      }),
    ],
    target: SCHEDULE,
    page: 'LinkedIn',
    times: {
      inbox_last: { at: '2026-10-21T11:26:00Z', style: 'ago' },
      inbox_next: { at: '2026-10-21T12:26:00Z', style: 'next' },
    },
    ...overrides,
  }
}

/** The netkeep chip's server parts while `netkeep serve` runs the background checks. */
export function netkeepChip(overrides: Partial<ServerChip> = {}): ServerChip {
  return {
    system: 'netkeep',
    name: 'netkeep',
    tone: 'ok',
    word: 'Connected',
    fact: '',
    parts: [
      part('checks', 'ok', 'Connected', 'netkeep serve is running them', {
        label: 'Background checks',
      }),
    ],
    target: { to: '/settings', hash: 'support' },
    page: 'Settings › Support',
    times: {},
    ...overrides,
  }
}

/** The whole answer of a healthy, quiet install. */
export function serverStatus(overrides: Partial<ServerStatus> = {}): ServerStatus {
  return {
    checked_at: '2026-10-21T12:00:00Z',
    gmail: gmailChip(),
    linkedin: linkedinChip(),
    netkeep: netkeepChip(),
    linkedin_paused: false,
    linkedin_run: null,
    campaign_sending: false,
    update_available: null,
    ...overrides,
  }
}
