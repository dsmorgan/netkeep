import { describe, expect, it } from 'vitest'

import type { Attention } from '@/features/attention/api'
import { formatWhen } from '@/features/linkedin/fields'
import type { BudgetStatus, Heat, LinkedInStatus, Run, Schedule } from '@/features/linkedin/types'
import { mailbox } from '@/features/mailboxes/test-support'

import {
  buildChips,
  buildProblems,
  needsYouCount,
  needsYouText,
  pickMailbox,
  type Read,
  type StatusReads,
} from './status'

// --- fixtures (fake people only) ---------------------------------------------------------

const ok = <T>(data: T): Read<T> => ({ state: 'ok', data })

function linkedinStatus(overrides: Partial<LinkedInStatus> = {}): LinkedInStatus {
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

function schedule(overrides: Partial<Schedule> = {}): Schedule {
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

function budget(
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

function heat(tripped = false): Heat {
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

function attention(overrides: Partial<Attention> = {}): Attention {
  return {
    checked_at: '2026-10-20T12:00:00Z',
    counts: { inbox: 0, dashboard: 0, waiting: 0, try_again: 0, triage: 0, imports: 0 },
    flags: { dashboard: null, gmail: null, linkedin: null, settings: null },
    activity: { state: 'idle', label: '', run_id: null, run_kind: null },
    title_total: 0,
    ...overrides,
  } as Attention
}

/** Every read answered, nothing wrong: a mailbox armed to send, scheduled runs armed. */
function reads(overrides: Partial<StatusReads> = {}): StatusReads {
  return {
    mailboxes: ok([mailbox({ arm: 'send', armed_at: '2026-09-21T12:00:00Z' })]),
    status: ok(linkedinStatus()),
    lastRun: undefined,
    schedule: ok(schedule()),
    budget: ok(budget({ count: 3, limit: 15, remaining: 12 })),
    heat: ok(heat()),
    attention: attention(),
    ...overrides,
  }
}

function chips(overrides: Partial<StatusReads> = {}) {
  const all = reads(overrides)
  return buildChips(all, buildProblems(all))
}

function chip(key: string, overrides: Partial<StatusReads> = {}) {
  const found = chips(overrides).find((candidate) => candidate.key === key)
  if (found === undefined) throw new Error(`no ${key} chip`)
  return {
    label: found.label,
    tone: found.tone,
    href: `${found.to}${found.hash ? `#${found.hash}` : ''}`,
  }
}

// --- the chips ----------------------------------------------------------------------------

describe('buildChips', () => {
  it('lists Gmail, the session, scheduled runs and prefills, in that order, when all is well', () => {
    expect(chips().map((c) => c.key)).toEqual(['gmail', 'session', 'schedule', 'prefills'])
  })

  it('adds the running job after the session, and problems last, only while there are some', () => {
    const run = { id: 9, kind: 'enrich', trigger: 'manual' } as Run
    const keys = chips({
      status: ok(linkedinStatus({ running_run_id: 9, session_flag: 'logged_out' })),
      lastRun: run,
    }).map((c) => c.key)
    expect(keys).toEqual(['gmail', 'session', 'running', 'schedule', 'prefills', 'problems'])
  })

  it('links every chip to the page or section that fixes it', () => {
    const all = chips({
      status: ok(linkedinStatus({ running_run_id: 9 })),
      heat: ok(heat(true)),
    })
    expect(Object.fromEntries(all.map((c) => [c.key, `${c.to}#${c.hash ?? ''}`]))).toEqual({
      gmail: '/gmail#',
      session: '/linkedin#session',
      running: '/linkedin#runs',
      schedule: '/linkedin#schedule',
      prefills: '/settings#linkedin-budget',
      problems: '/#needs-you',
    })
  })

  it('says each Gmail state, sending to Settings for anything to fix', () => {
    const gmail = (rows: ReturnType<typeof mailbox>[]) => chip('gmail', { mailboxes: ok(rows) })
    expect(gmail([mailbox({ arm: 'send', armed_at: 'x' })])).toEqual({
      label: 'Gmail sending',
      tone: 'ok',
      href: '/gmail',
    })
    expect(gmail([mailbox({ arm: 'draft', armed_at: 'x' })])).toEqual({
      label: 'Gmail drafts only',
      tone: 'ok',
      href: '/gmail',
    })
    expect(gmail([mailbox()])).toEqual({
      label: 'Gmail not armed',
      tone: 'warn',
      href: '/settings#gmail',
    })
    expect(gmail([mailbox({ status: 'reauth_required' })])).toEqual({
      label: 'Gmail needs re-authorizing',
      tone: 'bad',
      href: '/settings#gmail',
    })
    expect(gmail([mailbox({ status: 'disabled' })]).label).toBe('Gmail disconnected')
    expect(gmail([])).toEqual({
      label: 'Gmail not connected',
      tone: 'none',
      href: '/settings#gmail',
    })
  })

  it('says only what it knows about the LinkedIn session', () => {
    const session = (flag: string | null) =>
      chip('session', { status: ok(linkedinStatus({ session_flag: flag })) })
    // No flag is not a healthy session: nothing here checks the live one.
    expect(session(null)).toEqual({
      label: 'No LinkedIn session flag',
      tone: 'none',
      href: '/linkedin#session',
    })
    expect(session('checkpoint')).toMatchObject({ label: 'LinkedIn checkpoint', tone: 'bad' })
    expect(session('logged_out')).toMatchObject({ label: 'LinkedIn logged out', tone: 'bad' })
  })

  it('names the running job by its kind when the newest run is the one running', () => {
    const run = { id: 9, kind: 'enrich', trigger: 'manual' } as Run
    const status = ok(linkedinStatus({ running_run_id: 9 }))
    expect(chip('running', { status, lastRun: run })).toEqual({
      label: 'Enrichment running',
      tone: 'accent',
      href: '/linkedin#runs',
    })
    expect(chip('running', { status, lastRun: { ...run, id: 8 } }).label).toBe(
      'A LinkedIn run is running',
    )
  })

  it('says whether scheduled runs are armed, off, paused, or have no scheduler', () => {
    const plan = (overrides: Partial<Schedule>) =>
      chip('schedule', { schedule: ok(schedule(overrides)) })
    expect(plan({})).toMatchObject({ label: 'Scheduled runs armed', tone: 'ok' })
    expect(plan({ armed: false })).toMatchObject({ label: 'Scheduled runs off', tone: 'none' })
    expect(plan({ paused: true })).toMatchObject({ label: 'Scheduled runs paused', tone: 'warn' })
    expect(plan({ armed: false, paused: true }).label).toBe('Scheduled runs paused')
    expect(plan({ scheduler_running: false })).toMatchObject({
      label: 'No scheduler running',
      tone: 'bad',
    })
  })

  it('counts today’s prefills left from the li_prefills budget, amber once spent', () => {
    const prefills = (row: Parameters<typeof budget>[0]) =>
      chip('prefills', { budget: ok(budget(row)) })
    expect(prefills({ count: 13, limit: 15, remaining: 2 })).toEqual({
      label: '2 of 15 prefills left',
      tone: 'none',
      href: '/settings#linkedin-budget',
    })
    expect(prefills({ count: 15, limit: 15, remaining: 0 })).toMatchObject({
      label: '0 of 15 prefills left',
      tone: 'warn',
    })
    expect(prefills(null).label).toBe('Prefills left: unknown')
  })

  it('says it is checking, or could not check, instead of guessing', () => {
    const pending = { state: 'pending' } as const
    const failed = { state: 'error' } as const
    expect(
      chips({ mailboxes: pending, status: pending, schedule: pending, budget: pending }).map(
        (c) => c.label,
      ),
    ).toEqual([
      'Gmail: checking…',
      'LinkedIn: checking…',
      'Scheduled runs: checking…',
      'Prefills: checking…',
    ])
    expect(
      chips({ mailboxes: failed, status: failed, schedule: failed, budget: failed }).map(
        (c) => c.label,
      ),
    ).toEqual([
      'Gmail: couldn’t check',
      'LinkedIn: couldn’t check',
      'Scheduled runs: couldn’t check',
      'Prefills: couldn’t check',
    ])
  })

  it('counts the problems in words', () => {
    expect(chip('problems', { heat: ok(heat(true)) }).label).toBe('1 problem')
    expect(
      chip('problems', {
        heat: ok(heat(true)),
        status: ok(linkedinStatus({ session_flag: 'checkpoint' })),
      }).label,
    ).toBe('2 problems')
  })
})

// --- the problems -------------------------------------------------------------------------

describe('buildProblems', () => {
  it('finds nothing when nothing stopped', () => {
    expect(buildProblems(reads())).toEqual([])
  })

  it('lists a stopped session, a signed-out mailbox, heat and a breaker, most urgent first', () => {
    const problems = buildProblems(
      reads({
        status: ok(linkedinStatus({ session_flag: 'checkpoint' })),
        mailboxes: ok([mailbox({ status: 'reauth_required', status_reason: 'invalid_grant' })]),
        heat: ok(heat(true)),
        attention: attention({
          flags: {
            dashboard: null,
            gmail: null,
            linkedin: null,
            settings: 'The send breaker tripped.',
          },
        }),
      }),
    )
    expect(problems.map((p) => [p.key, `${p.to}#${p.hash ?? ''}`])).toEqual([
      ['session', '/linkedin#session'],
      ['mailbox', '/settings#gmail'],
      ['heat', '/linkedin#heat'],
      ['breaker', '/settings#posture'],
    ])
    expect(problems[0]?.title).toBe('LinkedIn asked for a checkpoint')
    expect(problems[1]?.mailboxId).toBe(3)
    // The reason Google gave, in words.
    expect(problems[1]?.detail).toMatch(/revoked, or the consent screen is still in Testing/)
    expect(problems[3]?.detail).toBe('The send breaker tripped.')
  })

  it('says when tripped heat lets scheduled runs resume, when the heat read knows', () => {
    const resumesAt = '2099-01-02T15:40:00Z'
    const [problem] = buildProblems(reads({ heat: ok({ ...heat(true), resumes_at: resumesAt }) }))
    expect(problem?.detail).toBe(
      `Scheduled LinkedIn runs are skipped until ${formatWhen(resumesAt)}.`,
    )
    const [unknown] = buildProblems(reads({ heat: ok(heat(true)) }))
    expect(unknown?.detail).toBe('Scheduled LinkedIn runs are skipped until it cools.')
  })

  it('says a logged-out session has its own fix', () => {
    const [problem] = buildProblems(
      reads({ status: ok(linkedinStatus({ session_flag: 'logged_out' })) }),
    )
    expect(problem?.title).toBe('Logged out of LinkedIn')
    expect(problem?.detail).toMatch(/netkeep preflight/)
  })

  it('adds nothing for a read that has not answered', () => {
    const pending = { state: 'pending' } as const
    expect(
      buildProblems(
        reads({
          status: pending,
          mailboxes: pending,
          heat: { state: 'error' },
          attention: undefined,
        }),
      ),
    ).toEqual([])
  })
})

// --- the mailbox the chip reads -----------------------------------------------------------

describe('pickMailbox', () => {
  it('reads the live mailbox, not a disabled one, when both are in the list', () => {
    const live = mailbox({ id: 2, status: 'ok' })
    expect(pickMailbox([mailbox({ id: 1, status: 'disabled' }), live])).toBe(live)
  })

  it('reads the most recently touched disabled one when none is live', () => {
    const newer = mailbox({ id: 2, status: 'disabled', updated_at: '2026-09-28T12:00:00Z' })
    const older = mailbox({ id: 1, status: 'disabled', updated_at: '2026-09-20T12:00:00Z' })
    expect(pickMailbox([older, newer])).toBe(newer)
    expect(pickMailbox([])).toBeUndefined()
  })
})

// --- the header line ----------------------------------------------------------------------

describe('needsYouCount', () => {
  it('adds the problems, the LinkedIn steps waiting, a held auto-send and the unhandled Inbox', () => {
    const problems = buildProblems(reads({ heat: ok(heat(true)) }))
    const counted = attention({
      counts: { inbox: 2, dashboard: 3, waiting: 2, try_again: 1, triage: 40, imports: 1 },
      flags: { dashboard: 'Auto-send is held.', gmail: null, linkedin: null, settings: null },
    })
    // Triage and draft imports are their pages' business, not the dashboard's.
    expect(needsYouCount(problems, counted)).toBe(1 + 3 + 1 + 2)
    expect(needsYouCount([], attention())).toBe(0)
    expect(needsYouCount(problems, undefined)).toBeNull()
  })

  it('reads as one plain line', () => {
    expect(needsYouText(null)).toBe('Checking what needs you…')
    expect(needsYouText(0)).toBe('Nothing needs you right now.')
    expect(needsYouText(1)).toBe('1 thing needs you.')
    expect(needsYouText(3)).toBe('3 things need you.')
  })
})
