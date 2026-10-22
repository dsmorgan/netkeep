import { describe, expect, it } from 'vitest'

import { formatWhen } from '@/features/linkedin/fields'

import {
  TONE_RANK,
  fill,
  fromServer,
  gmailStatus,
  leadPart,
  linkedinStatus,
  netkeepStatus,
  updatedText,
  type ChipReads,
  type NetkeepReads,
  type Part,
  type SystemStatus,
} from './roll-up'
import {
  gmailChip,
  linkedinChip,
  netkeepChip,
  ok,
  part as serverPart,
  serverStatus,
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
  return chip.parts.map((row) => [row.label, row.tone, row.detail])
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
})

describe('the server’s times', () => {
  it('writes each placeholder out at render time, in its style', () => {
    const times = {
      last: { at: '2026-10-21T11:57:00Z', style: 'ago' as const },
      next: { at: '2026-10-21T12:07:00Z', style: 'next' as const },
      when: { at: '2099-01-02T15:40:00Z', style: 'when' as const },
    }
    expect(fill('checked {last} · next {next}', times, NOW)).toBe(
      'checked 3 min ago · next in 7 min',
    )
    expect(fill('checked {last}', times, NOW + 60 * 60_000)).toBe('checked 1 h ago')
    expect(fill('runs wait until {when}', times, NOW)).toBe(
      `runs wait until ${formatWhen('2099-01-02T15:40:00Z')}`,
    )
    // A name the chip has no time for stays as it is.
    expect(fill('{other} and {last}', times, NOW)).toBe('{other} and 3 min ago')
  })
})

// --- Gmail and LinkedIn: the server's chips -----------------------------------------------

function chips(overrides: Partial<ChipReads> = {}): ChipReads {
  return { status: ok(serverStatus()), serverDown: false, ...overrides }
}

describe('gmailStatus', () => {
  it('reads the server’s chip, with its times written out', () => {
    const chip = gmailStatus(chips(), NOW)
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

  it('keeps a part’s own target, and no fact where the server sent none', () => {
    const chip = gmailStatus(chips(), NOW)
    expect(chip.parts[0]).toEqual({
      key: 'mailbox',
      label: 'Mailbox',
      tone: 'ok',
      word: 'Connected',
      detail: 'sender@example.com · connected',
    })
    const reauth = gmailChip({
      tone: 'bad',
      word: 'Re-authorize',
      fact: 'nothing sent or checked',
      target: { to: '/settings', hash: 'gmail' },
      page: 'Settings',
    })
    const signedOut = gmailStatus(chips({ status: ok(serverStatus({ gmail: reauth })) }), NOW)
    expect(face(signedOut)).toEqual({
      tone: 'bad',
      text: 'Gmail · Re-authorize · nothing sent or checked',
      href: '/settings#gmail',
    })
  })

  it('says it is checking, or couldn’t check, instead of guessing', () => {
    expect(face(gmailStatus(chips({ status: pending }), NOW))).toEqual({
      tone: 'off',
      text: 'Gmail · Checking…',
      href: '/gmail',
    })
    expect(face(gmailStatus(chips({ status: failed }), NOW)).text).toBe('Gmail · Couldn’t check')
  })

  it('reads Unknown while the server is down', () => {
    const chip = gmailStatus(chips({ serverDown: true }), NOW)
    expect(face(chip)).toEqual({ tone: 'off', text: 'Gmail · Unknown', href: '/gmail' })
    expect(rows(chip)).toEqual([['Gmail', 'off', 'Unknown while netkeep can’t be reached']])
  })
})

describe('linkedinStatus', () => {
  it('reads the server’s chip, with its times written out', () => {
    const chip = linkedinStatus(chips(), NOW)
    expect(face(chip)).toEqual({
      tone: 'ok',
      text: 'LinkedIn · Armed · inbox 34 min ago',
      href: '/linkedin#schedule',
    })
    expect(rows(chip)).toEqual([
      ['Session', 'ok', 'No session flag: no run has found LinkedIn logged out'],
      ['Scheduled runs', 'ok', 'Armed'],
      ['Inbox check', 'ok', 'Checked 34 min ago · next in 26 min'],
      ['Prefills today', 'ok', '2 of 15 left'],
      ['Heat', 'ok', 'Normal'],
    ])
  })

  it('writes out a running run’s start and heat’s resume time', () => {
    const resumesAt = '2099-01-02T15:40:00Z'
    const heat = linkedinChip({
      tone: 'bad',
      word: 'Heat high',
      fact: 'runs wait until {heat_resumes}',
      target: { to: '/linkedin', hash: 'heat' },
      times: { heat_resumes: { at: resumesAt, style: 'when' } },
    })
    expect(
      face(linkedinStatus(chips({ status: ok(serverStatus({ linkedin: heat })) }), NOW)),
    ).toEqual({
      tone: 'bad',
      text: `LinkedIn · Heat high · runs wait until ${formatWhen(resumesAt)}`,
      href: '/linkedin#heat',
    })
    const running = linkedinChip({
      tone: 'busy',
      word: 'Sync running',
      fact: 'started {run_started}',
      parts: [
        serverPart(
          'running',
          'busy',
          'Sync running',
          'Incremental connections sync · started {run_started}',
          {
            label: 'Running',
            fact: 'started {run_started}',
          },
        ),
      ],
      times: { run_started: { at: '2026-10-21T11:57:00Z', style: 'ago' } },
    })
    const chip = linkedinStatus(chips({ status: ok(serverStatus({ linkedin: running })) }), NOW)
    expect(face(chip).text).toBe('LinkedIn · Sync running · started 3 min ago')
    expect(chip.parts[0]?.detail).toBe('Incremental connections sync · started 3 min ago')
  })

  it('says it is checking, or couldn’t check, instead of guessing', () => {
    expect(face(linkedinStatus(chips({ status: pending }), NOW))).toEqual({
      tone: 'off',
      text: 'LinkedIn · Checking…',
      href: '/linkedin#session',
    })
    expect(rows(linkedinStatus(chips({ status: failed }), NOW))).toEqual([
      ['Session', 'off', 'Couldn’t be read; netkeep tries again shortly'],
    ])
  })

  it('reads Unknown while the server is down', () => {
    expect(face(linkedinStatus(chips({ serverDown: true }), NOW))).toEqual({
      tone: 'off',
      text: 'LinkedIn · Unknown',
      href: '/linkedin',
    })
  })
})

// --- netkeep: the UI's parts and the server's ----------------------------------------------

function app(overrides: Partial<NetkeepReads> = {}) {
  return netkeepStatus(
    {
      health: ok({ version: '0.1.0' }),
      stream: 'connected',
      status: ok(serverStatus()),
      ...overrides,
    },
    NOW,
  )
}

function withNetkeep(chip: Parameters<typeof netkeepChip>[0]) {
  return ok(serverStatus({ netkeep: netkeepChip(chip) }))
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
    const chip = app({ health: failed, stream: 'disconnected', status: failed })
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
    const chip = app({ status: failed })
    expect(face(chip)).toMatchObject({ tone: 'off', text: 'netkeep · Couldn’t check' })
    expect(chip.parts.find((row) => row.key === 'checks')?.label).toBe('Background checks')
    expect(face(app({ status: pending })).text).toBe('netkeep · Checking…')
  })

  it('warns when netkeep serve isn’t running the background checks', () => {
    const chip = app({
      status: withNetkeep({
        parts: [
          serverPart(
            'checks',
            'warn',
            'No background checks',
            'netkeep serve isn’t running, so nothing is checked in the background',
            { label: 'Background checks', fact: 'serve isn’t running' },
          ),
        ],
      }),
    })
    expect(face(chip)).toMatchObject({
      tone: 'warn',
      text: 'netkeep · No background checks · serve isn’t running',
    })
  })

  it('lets the server’s Keychain part lead its background checks, and a down server lead both', () => {
    const status = withNetkeep({
      parts: [
        serverPart('checks', 'warn', 'No background checks', 'x', { fact: 'serve isn’t running' }),
        serverPart('keychain', 'warn', 'Keychain refused', 'y', {
          label: 'Keychain',
          fact: 'key in memory',
        }),
      ],
    })
    expect(face(app({ status })).text).toBe('netkeep · Keychain refused · key in memory')
    expect(face(app({ status, stream: 'disconnected' })).text).toBe(
      'netkeep · Keychain refused · key in memory',
    )
  })

  it('reads grey and not live while the event stream reconnects', () => {
    expect(face(app({ stream: 'disconnected' }))).toMatchObject({
      tone: 'off',
      text: 'netkeep · Not live · reconnecting',
    })
  })
})

describe('fromServer', () => {
  it('keeps the server’s tone, word, page and target as they are', () => {
    const chip = fromServer(
      linkedinChip({
        tone: 'warn',
        word: 'Auto-send held',
        fact: '',
        target: { to: '/', hash: 'needs-you' },
        page: 'Dashboard',
      }),
      NOW,
    )
    expect(face(chip)).toEqual({
      tone: 'warn',
      text: 'LinkedIn · Auto-send held',
      href: '/#needs-you',
    })
    expect(chip.page).toBe('Dashboard')
  })
})

describe('updatedText', () => {
  it('says how long ago the newest read answered', () => {
    expect(updatedText(0, NOW)).toBe('Not updated yet')
    expect(updatedText(NOW - 10_000, NOW)).toBe('Updated just now')
    expect(updatedText(NOW - 3 * 60_000, NOW)).toBe('Updated 3 min ago')
  })
})
