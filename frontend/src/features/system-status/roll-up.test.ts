import { describe, expect, it } from 'vitest'

import { formatWhen } from '@/features/linkedin/fields'
import { mailbox } from '@/features/mailboxes/test-support'

import {
  CHECK_TONE,
  TONE_RANK,
  gmailStatus,
  leadPart,
  linkedinStatus,
  netkeepStatus,
  pickMailbox,
  progressText,
  updatedText,
  type GmailReads,
  type LinkedInReads,
  type NetkeepReads,
  type Part,
  type SystemStatus,
} from './roll-up'
import {
  attention,
  budget,
  check,
  heat,
  linkedinStatus as status,
  ok,
  pollStatus,
  run,
  schedule,
} from './test-support'

const NOW = Date.parse('2026-10-21T12:00:00Z')
const pending = { state: 'pending' } as const
const failed = { state: 'error' } as const

/** The chip as it reads, its tone, and where its button goes. */
function face(chip: SystemStatus) {
  return {
    tone: chip.tone,
    text: `${chip.name} · ${chip.word}${chip.fact === '' ? '' : ` · ${chip.fact}`}`,
    href: `${chip.target.to}${chip.target.hash ? `#${chip.target.hash}` : ''}`,
  }
}

function rows(chip: SystemStatus) {
  return chip.parts.map((part) => [part.label, part.tone, part.detail])
}

function part(key: string, tone: Part['tone']): Part {
  return { key, label: key, tone, word: key, detail: key }
}

describe('the severity scale', () => {
  it('ranks off above ok, and stopped worst', () => {
    const order = Object.entries(TONE_RANK)
      .sort(([, a], [, b]) => a - b)
      .map(([tone]) => tone)
    expect(order).toEqual(['ok', 'off', 'busy', 'warn', 'bad'])
  })

  it('takes the worst part, and among equals the first in lead order, then the list', () => {
    const parts = [part('a', 'ok'), part('b', 'warn'), part('c', 'warn'), part('d', 'busy')]
    expect(leadPart(parts)?.key).toBe('b')
    expect(leadPart(parts, ['c', 'b'])?.key).toBe('c')
    expect(leadPart([part('a', 'ok'), part('b', 'ok')], ['b'])?.key).toBe('b')
    expect(leadPart([])).toBeUndefined()
  })

  it('reads a check that waits on its own as working, and one serve doesn’t run as off', () => {
    expect(CHECK_TONE).toEqual({
      scheduled: 'ok',
      due: 'ok',
      idle: 'ok',
      outside_hours: 'ok',
      paused: 'warn',
      blocked: 'warn',
      off: 'off',
      not_running: 'off',
      not_wired: 'off',
    })
  })
})

// --- Gmail ----------------------------------------------------------------------------

function gmail(overrides: Partial<GmailReads> = {}) {
  return gmailStatus(
    {
      mailboxes: ok([mailbox({ arm: 'send', armed_at: '2026-09-21T12:00:00Z' })]),
      poll: ok(pollStatus()),
      sending: false,
      serverDown: false,
      ...overrides,
    },
    NOW,
  )
}

describe('gmailStatus', () => {
  it('says how it sends and when replies were last checked, all well', () => {
    const chip = gmail()
    expect(face(chip)).toEqual({
      tone: 'ok',
      text: 'Gmail · Sending · checked 2 min ago',
      href: '/gmail',
    })
    expect(rows(chip)).toEqual([
      ['Mailbox', 'ok', 'sender@example.com · connected'],
      ['Sending', 'ok', 'Armed to send'],
      ['Reply check', 'ok', 'Checked 2 min ago · next in 8 min'],
    ])
    expect(chip.page).toBe('Gmail')
  })

  it('says drafts only, and warns when the mailbox isn’t armed', () => {
    expect(face(gmail({ mailboxes: ok([mailbox({ arm: 'draft' })]) })).text).toBe(
      'Gmail · Drafts only · checked 2 min ago',
    )
    expect(face(gmail({ mailboxes: ok([mailbox()]) }))).toEqual({
      tone: 'warn',
      text: 'Gmail · Not armed · checked 2 min ago',
      href: '/settings#gmail',
    })
  })

  it('stops, and sends you to Settings, when Google signed the mailbox out', () => {
    const chip = gmail({
      mailboxes: ok([mailbox({ status: 'reauth_required', status_reason: 'invalid_grant' })]),
    })
    expect(face(chip)).toEqual({
      tone: 'bad',
      text: 'Gmail · Re-authorize · nothing sent or checked',
      href: '/settings#gmail',
    })
    expect(chip.page).toBe('Settings')
    expect(chip.parts[0]?.detail).toMatch(/^Google signed netkeep out of sender@example\.com\./)
    expect(chip.parts[0]?.detail).toMatch(/revoked, or the consent screen is still in Testing/)
    // Nothing to say about arming a mailbox that can't be used.
    expect(chip.parts.map((row) => row.key)).toEqual(['mailbox', 'replies'])
  })

  it('reads grey when no mailbox is connected, or it was disconnected', () => {
    const none = gmail({
      mailboxes: ok([]),
      poll: ok(pollStatus({ items: [check({ state: 'off', last_at: null, next_at: null })] })),
    })
    expect(face(none)).toEqual({ tone: 'off', text: 'Gmail · Not set up', href: '/settings#gmail' })
    expect(face(gmail({ mailboxes: ok([mailbox({ status: 'disabled' })]) }))).toMatchObject({
      tone: 'off',
      text: 'Gmail · Disconnected',
    })
  })

  it('reads the live mailbox, not a disabled one, when both are in the list', () => {
    const live = mailbox({ id: 2, status: 'ok' })
    expect(pickMailbox([mailbox({ id: 1, status: 'disabled' }), live])).toBe(live)
    const newer = mailbox({ id: 2, status: 'disabled', updated_at: '2026-09-28T12:00:00Z' })
    const older = mailbox({ id: 1, status: 'disabled', updated_at: '2026-09-20T12:00:00Z' })
    expect(pickMailbox([older, newer])).toBe(newer)
    expect(pickMailbox([])).toBeUndefined()
  })

  it('spins while a campaign send batch runs', () => {
    const chip = gmail({ sending: true })
    expect(face(chip)).toEqual({
      tone: 'busy',
      text: 'Gmail · Sending now · campaign batch',
      href: '/gmail',
    })
    expect(chip.parts.find((row) => row.key === 'batch')?.tone).toBe('busy')
  })

  it('says why the reply check has no next time', () => {
    const chip = gmail({
      poll: ok(
        pollStatus({
          items: [check({ state: 'paused', next_at: null, reason: 'Sending hours are over' })],
        }),
      ),
    })
    expect(face(chip).text).toBe('Gmail · Paused · checked 2 min ago')
    expect(chip.parts.at(-1)?.detail).toBe('Checked 2 min ago · paused. Sending hours are over')
    expect(face(gmail({ poll: ok(pollStatus({ items: [check({ last_at: null })] })) })).text).toBe(
      'Gmail · Sending · not checked yet',
    )
  })

  it('says it is checking, or couldn’t check, instead of guessing', () => {
    expect(face(gmail({ mailboxes: pending, poll: pending }))).toEqual({
      tone: 'off',
      text: 'Gmail · Checking…',
      href: '/gmail',
    })
    expect(face(gmail({ poll: failed })).text).toBe('Gmail · Couldn’t check')
  })

  it('shows a draft held for a contact you shouldn’t email, from /attention', () => {
    const flag =
      'A Gmail draft waits for a contact you shouldn’t email; see Leftover drafts on the Gmail page.'
    const flags = { dashboard: null, gmail: flag, linkedin: null, settings: null }
    const chip = gmail({ attention: attention({ flags }) })
    expect(face(chip)).toEqual({
      tone: 'warn',
      text: 'Gmail · Draft held',
      href: '/gmail#leftover-drafts',
    })
    expect(chip.parts.find((row) => row.key === 'flag')?.detail).toBe(flag)
    // A sign-in flag the mailbox part already says adds no row.
    const signIn = { ...flags, gmail: 'A mailbox needs you to sign in again.' }
    const reauth = gmail({
      mailboxes: ok([mailbox({ status: 'reauth_required' })]),
      attention: attention({ flags: signIn }),
    })
    expect(reauth.parts.map((row) => row.key)).toEqual(['mailbox', 'replies'])
  })

  it('reads Unknown while the server is down', () => {
    const chip = gmail({ serverDown: true })
    expect(face(chip)).toEqual({ tone: 'off', text: 'Gmail · Unknown', href: '/gmail' })
    expect(rows(chip)).toEqual([['Gmail', 'off', 'Unknown while netkeep can’t be reached']])
  })
})

// --- LinkedIn -------------------------------------------------------------------------

function linkedin(overrides: Partial<LinkedInReads> = {}) {
  return linkedinStatus(
    {
      status: ok(status()),
      lastRun: undefined,
      schedule: ok(schedule({ jobs: [] })),
      budget: ok(budget({ count: 3, limit: 15, remaining: 12 })),
      heat: ok(heat()),
      poll: ok(pollStatus()),
      attention: attention(),
      serverDown: false,
      ...overrides,
    },
    NOW,
  )
}

describe('linkedinStatus', () => {
  it('says scheduled runs are armed and when the inbox was last checked, all well', () => {
    const chip = linkedin()
    expect(face(chip)).toEqual({
      tone: 'ok',
      text: 'LinkedIn · Armed · inbox 34 min ago',
      href: '/linkedin#schedule',
    })
    // No flag is not a healthy session: nothing here checks the live one.
    expect(rows(chip)).toEqual([
      ['Session', 'ok', 'No session flag: no run has found LinkedIn logged out'],
      ['Scheduled runs', 'ok', 'Armed'],
      ['Inbox check', 'ok', 'Checked 34 min ago · next in 26 min'],
      ['Prefills today', 'ok', '12 of 15 left'],
      ['Heat', 'ok', 'Normal'],
    ])
  })

  it('names the next scheduled run', () => {
    const chip = linkedin({
      schedule: ok(
        schedule({
          jobs: [
            { kind: 'enrich', interval_hours: 24, next_due: '2026-10-21T12:40:00Z' },
            { kind: 'inbox', interval_hours: 1, next_due: '2026-10-21T12:20:00Z' },
            { kind: 'connections_full', interval_hours: 168, next_due: null },
          ],
        }),
      ),
    })
    expect(chip.parts[1]?.detail).toBe('Armed · next run in 20 min')
  })

  it('spins with the running run’s progress, linking to the runs', () => {
    const running = ok(status({ running_run_id: 9 }))
    const chip = linkedin({ status: running, lastRun: run({ planned: 626, completed: 340 }) })
    expect(face(chip)).toEqual({
      tone: 'busy',
      text: 'LinkedIn · Enrichment running · 340 of 626',
      href: '/linkedin#runs',
    })
    expect(chip.parts[1]).toMatchObject({
      label: 'Running',
      detail: 'Enrichment · 340 of 626 · started 3 min ago',
    })
    expect(
      face(linkedin({ status: running, lastRun: run({ kind: 'connections_incremental' }) })).text,
    ).toBe('LinkedIn · Sync running · started 3 min ago')
    expect(
      face(
        linkedin({
          status: running,
          lastRun: run({ kind: 'message_send', trigger: 'scheduled' }),
        }),
      ).text,
    ).toBe('LinkedIn · Auto-send running · started 3 min ago')
    // The newest run isn't the running one: say only that one runs.
    expect(face(linkedin({ status: running, lastRun: run({ id: 8 }) })).text).toBe(
      'LinkedIn · Running',
    )
  })

  it('moves the running count with each run.progress event, and reads a sync’s count', () => {
    const running = ok(status({ running_run_id: 9 }))
    const sync = run({
      kind: 'connections_full',
      progress: { mode: 'full', pages: 4, connections: 120, total: 626 },
    })
    expect(face(linkedin({ status: running, lastRun: sync })).text).toBe(
      'LinkedIn · Full sync running · 120 of 626',
    )
    const live = { run_id: 9, mode: 'full', pages: 9, connections: 340, total: 626 }
    expect(face(linkedin({ status: running, lastRun: sync, progress: live })).text).toBe(
      'LinkedIn · Full sync running · 340 of 626',
    )
    // Another run's event is not this one's.
    expect(
      face(linkedin({ status: running, lastRun: sync, progress: { ...live, run_id: 8 } })).text,
    ).toBe('LinkedIn · Full sync running · 120 of 626')
  })

  it('counts only the progress shapes it knows', () => {
    expect(progressText({ planned: 50, visited: 12, harvested: 10 })).toBe('12 of 50')
    expect(progressText({ connections: 340, total: 626 })).toBe('340 of 626')
    expect(progressText({ connections: 340, total: null })).toBeNull()
    expect(progressText({ planned: 0, visited: 0 })).toBeNull()
    expect(progressText({ visited: '3', planned: 5 })).toBeNull()
    expect(progressText(null)).toBeNull()
  })

  it('stops for a logged-out session or a checkpoint, above everything else', () => {
    const out = linkedin({
      status: ok(status({ session_flag: 'logged_out', running_run_id: 9 })),
      schedule: ok(schedule({ paused: true })),
    })
    expect(face(out)).toEqual({
      tone: 'bad',
      text: 'LinkedIn · Logged out · runs stopped',
      href: '/linkedin#session',
    })
    expect(out.page).toBe('LinkedIn')
    expect(face(linkedin({ status: ok(status({ session_flag: 'checkpoint' })) })).text).toBe(
      'LinkedIn · Checkpoint · runs stopped',
    )
  })

  it('says whether scheduled runs are paused, off, or have no scheduler', () => {
    const plan = (overrides: Parameters<typeof schedule>[0]) =>
      face(linkedin({ schedule: ok(schedule(overrides)) }))
    expect(plan({ paused: true })).toEqual({
      tone: 'warn',
      text: 'LinkedIn · Paused · inbox 34 min ago',
      href: '/linkedin#schedule',
    })
    expect(plan({ armed: false })).toMatchObject({ tone: 'off', text: 'LinkedIn · Off' })
    expect(plan({ armed: false, paused: true }).text).toMatch(/Paused/)
    expect(plan({ scheduler_running: false })).toMatchObject({
      tone: 'bad',
      text: 'LinkedIn · No scheduler · runs don’t start',
    })
  })

  it('warns once today’s prefills are spent, linking to the budget', () => {
    const chip = linkedin({ budget: ok(budget({ count: 15, limit: 15, remaining: 0 })) })
    expect(face(chip)).toEqual({
      tone: 'warn',
      text: 'LinkedIn · No prefills left · until tomorrow',
      href: '/linkedin#budget',
    })
    // No li_prefills row: no part, rather than a guess.
    expect(linkedin({ budget: ok(budget(null)) }).parts.map((row) => row.key)).not.toContain(
      'prefills',
    )
  })

  it('stops for heat over its threshold, saying when runs resume', () => {
    const resumesAt = '2099-01-02T15:40:00Z'
    const chip = linkedin({ heat: ok({ ...heat(true), resumes_at: resumesAt }) })
    expect(face(chip)).toEqual({
      tone: 'bad',
      text: `LinkedIn · Heat high · runs wait until ${formatWhen(resumesAt)}`,
      href: '/linkedin#heat',
    })
  })

  it('warns while auto-send is held, linking to Resume under Needs you', () => {
    const chip = linkedin({
      attention: attention({
        flags: {
          dashboard: 'Auto-send is held until you resume it.',
          gmail: null,
          linkedin: null,
          settings: null,
        },
      }),
    })
    expect(face(chip)).toEqual({
      tone: 'warn',
      text: 'LinkedIn · Auto-send held',
      href: '/#needs-you',
    })
    expect(chip.page).toBe('Dashboard')
    expect(chip.parts.at(-1)?.detail).toBe('Auto-send is held until you resume it.')
  })

  it('stops for a tripped breaker, linking to Settings › Posture', () => {
    const breaker =
      'A breaker is tripped, so some scheduled runs can’t start; reset it on the Settings page.'
    const chip = linkedin({
      attention: attention({
        flags: { dashboard: null, gmail: null, linkedin: null, settings: breaker },
        activity: { state: 'stopped', label: breaker, run_id: null, run_kind: null },
      }),
    })
    expect(face(chip)).toEqual({
      tone: 'bad',
      text: 'LinkedIn · Breaker tripped · some runs can’t start',
      href: '/settings#posture',
    })
    expect(chip.page).toBe('Settings')
    expect(chip.parts.map((row) => row.key)).not.toContain('blocked')
  })

  it('never reads green while /attention says LinkedIn is blocked', () => {
    // The session flag was set outside a run, and /linkedin/status hasn't caught up.
    const label = 'The LinkedIn session is flagged; clear it on the LinkedIn page.'
    const chip = linkedin({
      attention: attention({
        flags: { dashboard: null, gmail: null, linkedin: label, settings: null },
        activity: { state: 'stopped', label, run_id: null, run_kind: null },
      }),
    })
    expect(face(chip)).toMatchObject({ tone: 'warn', text: 'LinkedIn · Blocked' })
    expect(chip.parts.at(-1)).toMatchObject({ label: 'Blocked', detail: label })
    // Once a part explains it, that part speaks and there is no extra row.
    const caught = linkedin({
      status: ok(status({ session_flag: 'logged_out' })),
      attention: attention({
        flags: { dashboard: null, gmail: null, linkedin: label, settings: null },
        activity: { state: 'stopped', label, run_id: null, run_kind: null },
      }),
    })
    expect(face(caught).text).toBe('LinkedIn · Logged out · runs stopped')
    expect(caught.parts.map((row) => row.key)).not.toContain('blocked')
  })

  it('says it is checking, or couldn’t check, instead of guessing', () => {
    const chip = linkedin({ status: pending, schedule: pending, budget: failed })
    expect(face(chip)).toEqual({
      tone: 'off',
      text: 'LinkedIn · Checking…',
      href: '/linkedin#session',
    })
    expect(rows(chip).map(([label, , detail]) => `${label}: ${detail}`)).toContain(
      'Prefills today: Couldn’t be read; netkeep tries again shortly',
    )
  })

  it('reads Unknown while the server is down', () => {
    expect(face(linkedin({ serverDown: true }))).toEqual({
      tone: 'off',
      text: 'LinkedIn · Unknown',
      href: '/linkedin',
    })
  })
})

// --- netkeep --------------------------------------------------------------------------

function app(overrides: Partial<NetkeepReads> = {}) {
  return netkeepStatus({
    health: ok({ version: '0.1.0' }),
    stream: 'connected',
    poll: ok(pollStatus()),
    keyPersisted: true,
    ...overrides,
  })
}

describe('netkeepStatus', () => {
  it('says connected and live, all well, linking to Settings › Support', () => {
    const chip = app()
    expect(face(chip)).toEqual({
      tone: 'ok',
      text: 'netkeep · Connected · live',
      href: '/settings#support',
    })
    expect(chip.page).toBe('Settings › Support')
    expect(rows(chip)).toEqual([
      ['Server', 'ok', 'Reachable · 0.1.0'],
      ['Live updates', 'ok', 'Connected: pages update as things happen'],
      ['Background checks', 'ok', 'netkeep serve is running them'],
    ])
  })

  it('reads Unreachable, retrying, in red while the server is down', () => {
    const chip = app({ health: failed, stream: 'disconnected' })
    expect(face(chip)).toEqual({
      tone: 'bad',
      text: 'netkeep · Unreachable · retrying',
      href: '/settings#support',
    })
    expect(chip.page).toBe('Settings › Support')
    expect(rows(chip)).toEqual([
      ['Server', 'bad', 'Can’t reach netkeep · retrying every 15 s'],
      ['Live updates', 'off', 'Disconnected'],
    ])
  })

  it('offers a reload, not a page that can’t load, while the server is down', () => {
    expect(app({ health: failed }).reload).toBe(true)
    expect(app().reload).toBeUndefined()
  })

  it('never reads green when the background checks can’t be read', () => {
    const chip = app({ poll: failed })
    expect(face(chip)).toMatchObject({ tone: 'off', text: 'netkeep · Couldn’t check' })
    expect(chip.parts.find((row) => row.key === 'checks')?.label).toBe('Background checks')
  })

  it('warns when netkeep serve isn’t running the background checks', () => {
    const chip = app({ poll: ok(pollStatus({ background_running: false })) })
    expect(face(chip)).toMatchObject({
      tone: 'warn',
      text: 'netkeep · No background checks · serve isn’t running',
    })
  })

  it('warns when the Keychain refused the key, before the background checks', () => {
    const chip = app({
      keyPersisted: false,
      poll: ok(pollStatus({ background_running: false })),
    })
    expect(face(chip).text).toBe('netkeep · Keychain refused · key in memory')
    expect(chip.parts.at(-1)).toMatchObject({
      label: 'Keychain',
      tone: 'warn',
      detail: 'Refused · the local API key lives in memory until netkeep stops',
    })
  })

  it('reads grey and not live while the event stream reconnects', () => {
    expect(face(app({ stream: 'disconnected' }))).toMatchObject({
      tone: 'off',
      text: 'netkeep · Not live · reconnecting',
    })
  })
})

describe('updatedText', () => {
  it('says how long ago the newest read answered', () => {
    expect(updatedText(0, NOW)).toBe('Not updated yet')
    expect(updatedText(NOW - 10_000, NOW)).toBe('Updated just now')
    expect(updatedText(NOW - 3 * 60_000, NOW)).toBe('Updated 3 min ago')
  })
})
