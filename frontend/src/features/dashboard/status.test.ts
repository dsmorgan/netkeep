import { describe, expect, it } from 'vitest'

import { formatWhen } from '@/features/linkedin/fields'
import { mailbox } from '@/features/mailboxes/test-support'
import { attention, heat, linkedinStatus, ok } from '@/features/system-status/test-support'

import { buildProblems, needsYouCount, needsYouText, pickMailbox, type StatusReads } from './status'

// --- fixtures (fake people only) ---------------------------------------------------------

/** Every read answered, nothing wrong: a mailbox armed to send, no flag, heat normal. */
function reads(overrides: Partial<StatusReads> = {}): StatusReads {
  return {
    mailboxes: ok([mailbox({ arm: 'send', armed_at: '2026-09-21T12:00:00Z' })]),
    status: ok(linkedinStatus()),
    heat: ok(heat()),
    attention: attention(),
    ...overrides,
  }
}

// --- the problems -------------------------------------------------------------------------

describe('pickMailbox', () => {
  it('reads the live mailbox, not a disabled one, when both are in the list', () => {
    const live = mailbox({ id: 2, status: 'ok' })
    expect(pickMailbox([mailbox({ id: 1, status: 'disabled' }), live])).toBe(live)
    const newer = mailbox({ id: 2, status: 'disabled', updated_at: '2026-09-28T12:00:00Z' })
    const older = mailbox({ id: 1, status: 'disabled', updated_at: '2026-09-20T12:00:00Z' })
    expect(pickMailbox([older, newer])).toBe(newer)
    expect(pickMailbox([])).toBeUndefined()
  })
})

describe('buildProblems', () => {
  it('finds nothing when nothing stopped', () => {
    expect(buildProblems(reads())).toEqual([])
  })

  it('lists a stopped session, a signed-out mailbox, heat and a breaker, most urgent first', () => {
    const problems = buildProblems(
      reads({
        status: ok(linkedinStatus({ session_flag: 'checkpoint', heat_tripped: true })),
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
    const tripped = ok(linkedinStatus({ heat_tripped: true }))
    const resumesAt = '2099-01-02T15:40:00Z'
    const [problem] = buildProblems(
      reads({ status: tripped, heat: ok({ ...heat(true), resumes_at: resumesAt }) }),
    )
    expect(problem?.detail).toBe(
      `Scheduled LinkedIn runs are skipped until ${formatWhen(resumesAt)}.`,
    )
    const [unknown] = buildProblems(reads({ status: tripped, heat: ok(heat(true)) }))
    expect(unknown?.detail).toBe('Scheduled LinkedIn runs are skipped until it cools.')
    // Not read yet (it is read only once status says tripped), or not caught up: no time.
    for (const late of [{ state: 'pending' } as const, ok({ ...heat(), resumes_at: resumesAt })]) {
      const [waiting] = buildProblems(reads({ status: tripped, heat: late }))
      expect(waiting?.detail).toBe('Scheduled LinkedIn runs are skipped until it cools.')
    }
  })

  it('takes whether heat tripped from the status read, not the heat read (#752)', () => {
    expect(buildProblems(reads({ heat: ok(heat(true)) }))).toEqual([])
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

// --- the header line ----------------------------------------------------------------------

describe('needsYouCount', () => {
  it('adds the problems, the LinkedIn steps waiting, a held auto-send and the unhandled Inbox', () => {
    const problems = buildProblems(
      reads({ status: ok(linkedinStatus({ heat_tripped: true })), heat: ok(heat(true)) }),
    )
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
