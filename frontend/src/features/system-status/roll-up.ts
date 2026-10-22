/**
 * The top bar's three status chips (#751), Gmail, LinkedIn and netkeep, as pure
 * functions of the reads they come from, so every state is tested without rendering.
 *
 * The server rolls up its own parts (`GET /system-status`, #761, ADR 0014 rule 1), and
 * this adds only what the UI alone can know, by the same rule: whether the server
 * answers, and live updates. Each chip's parts have a tone on one scale, the chip takes
 * the worst, and its word and fact come from the part that set it. A read still in
 * flight or failed is a part of its own that says so, never a guess. When the server
 * can't be reached, Gmail and LinkedIn say "Unknown" rather than their last answer.
 *
 * The server sends times as absolute UTC timestamps, named by `{placeholders}` in a
 * fact or a detail; they are written here at render time, so they never go stale.
 */
import type { UseQueryResult } from '@tanstack/react-query'

import type { components } from '@/api/schema'
import type { EventStreamStatus } from '@/features/events/use-event-stream'
import { formatWhen } from '@/features/linkedin/fields'
import { ago, nextText } from '@/features/poll-status/format'

export type ServerStatus = components['schemas']['SystemStatusOut']
export type ServerChip = components['schemas']['StatusChipOut']
export type ServerTime = components['schemas']['StatusTimeOut']

/**
 * One severity scale for every chip: working as set up, busy right now, needs you
 * soon, stopped, and off or not set up.
 */
export type StatusTone = 'ok' | 'busy' | 'warn' | 'bad' | 'off'

/**
 * Worst wins, in this order. Off outranks ok: a chip with a part that is off or not
 * set up is not working in full, so it says so rather than showing green.
 */
export const TONE_RANK: Record<StatusTone, number> = { ok: 0, off: 1, busy: 2, warn: 3, bad: 4 }

/** Each tone in words, for a screen reader beside its icon. */
export const TONE_TEXT: Record<StatusTone, string> = {
  ok: 'Working',
  busy: 'Busy',
  warn: 'Needs you soon',
  bad: 'Stopped',
  off: 'Off',
}

/** One query's answer as the chips need it: still in flight, failed, or its data. */
export type Read<T> = { state: 'pending' } | { state: 'error' } | { state: 'ok'; data: T }

/** A query's state as a {@link Read}. */
export function read<T>(query: UseQueryResult<T>): Read<T> {
  if (query.isPending) return { state: 'pending' }
  if (query.isError) return { state: 'error' }
  return { state: 'ok', data: query.data }
}

/** Where a chip or a problem sends you: a page, and the section on it. */
export interface Target {
  to: '/' | '/gmail' | '/linkedin' | '/settings'
  hash?: string
}

export interface Part {
  key: string
  /** The row's name in the popover. */
  label: string
  tone: StatusTone
  /** The row's detail in the popover. */
  detail: string
  /** What the chip says when this part sets its tone. */
  word: string
  /** The chip's muted fact when this part sets it; the chip's own fact when absent. */
  fact?: string
  /** Where this part is fixed, when it sets the chip's tone. */
  target?: Target
}

export type SystemKey = 'gmail' | 'linkedin' | 'netkeep'

export interface SystemStatus {
  system: SystemKey
  name: string
  tone: StatusTone
  word: string
  fact: string
  parts: Part[]
  target: Target
  /** The target page's name, for the popover's button. */
  page: string
  /** No page can load (the server is down): the popover offers a reload instead of a link. */
  reload?: boolean
}

export const GMAIL_SETTINGS: Target = { to: '/settings', hash: 'gmail' }
export const SUPPORT: Target = { to: '/settings', hash: 'support' }

const PAGE_NAMES: Record<string, string> = {
  '/': 'Dashboard',
  '/gmail': 'Gmail',
  '/linkedin': 'LinkedIn',
  '/settings': 'Settings',
  '/settings#support': 'Settings › Support',
}

function pageName(target: Target): string {
  return PAGE_NAMES[`${target.to}#${target.hash ?? ''}`] ?? PAGE_NAMES[target.to] ?? target.to
}

/**
 * The part that sets the chip: the worst tone, and among parts with that tone the
 * first in `lead` order (then the popover's order), so the most urgent of two problems
 * speaks. A chip that is all well says its `calm` part instead (`rollUp`).
 */
export function leadPart(parts: Part[], lead: readonly string[] = []): Part | undefined {
  const order = (part: Part) => {
    const at = lead.indexOf(part.key)
    return at === -1 ? lead.length + parts.indexOf(part) : at
  }
  return [...parts].sort((a, b) => TONE_RANK[b.tone] - TONE_RANK[a.tone] || order(a) - order(b))[0]
}

function rollUp(
  system: SystemKey,
  name: string,
  parts: Part[],
  {
    lead,
    calm,
    fact,
    target,
  }: {
    lead: readonly string[]
    /** The part that speaks for the chip when every part is ok. */
    calm?: string
    fact: string
    target: Target
  },
): SystemStatus {
  const worst = leadPart(parts, lead)
  const top = worst?.tone === 'ok' ? (parts.find((part) => part.key === calm) ?? worst) : worst
  const to = top?.target ?? target
  return {
    system,
    name,
    tone: top?.tone ?? 'off',
    word: top?.word ?? 'Unknown',
    fact: top?.fact ?? fact,
    parts,
    target: to,
    page: pageName(to),
  }
}

/** A part whose read has not answered, or failed: grey, and saying which. */
function unread(
  key: string,
  label: string,
  read: { state: 'pending' | 'error' },
  target?: Target,
): Part {
  const pending = read.state === 'pending'
  return {
    key,
    label,
    tone: 'off',
    word: pending ? 'Checking…' : 'Couldn’t check',
    fact: '',
    detail: pending ? 'Checking…' : 'Couldn’t be read; netkeep tries again shortly',
    ...(target === undefined ? {} : { target }),
  }
}

/** A chip whose system can't be known while the server is down. */
function unknown(system: SystemKey, name: string, target: Target): SystemStatus {
  return rollUp(
    system,
    name,
    [
      {
        key: 'unknown',
        label: name,
        tone: 'off',
        word: 'Unknown',
        fact: '',
        detail: 'Unknown while netkeep can’t be reached',
      },
    ],
    { lead: [], fact: '', target },
  )
}

// --- the server's answer --------------------------------------------------------------

/** A time as its style says: "3 min ago", "in 7 min" or "14:20", a date and time. */
export function timeText(time: ServerTime, now: number): string {
  switch (time.style) {
    case 'ago':
      return ago(time.at, now)
    case 'next':
      return nextText(time.at, now)
    case 'when':
      return formatWhen(time.at)
  }
}

/** `text` with each `{name}` the chip's `times` holds written out at `now`. */
export function fill(text: string, times: ServerChip['times'], now: number): string {
  return text.replace(/\{(\w+)\}/g, (placeholder, name: string) => {
    const time = times[name]
    return time === undefined ? placeholder : timeText(time, now)
  })
}

function target(raw: components['schemas']['StatusTargetOut']): Target {
  return raw.hash === null ? { to: raw.to } : { to: raw.to, hash: raw.hash }
}

function serverParts(chip: ServerChip, now: number): Part[] {
  return chip.parts.map((raw) => ({
    key: raw.key,
    label: raw.label,
    tone: raw.tone,
    word: raw.word,
    detail: fill(raw.detail, chip.times, now),
    ...(raw.fact === null ? {} : { fact: fill(raw.fact, chip.times, now) }),
    ...(raw.target === null ? {} : { target: target(raw.target) }),
  }))
}

/** A chip the server rolled up, with its times written out at `now`. */
export function fromServer(chip: ServerChip, now: number): SystemStatus {
  const to = target(chip.target)
  return {
    system: chip.system,
    name: chip.name,
    tone: chip.tone,
    word: chip.word,
    fact: fill(chip.fact, chip.times, now),
    parts: serverParts(chip, now),
    target: to,
    page: chip.page,
  }
}

export interface ChipReads {
  /** `GET /system-status`. */
  status: Read<ServerStatus>
  /** The health check failed: the server can't be reached. */
  serverDown: boolean
}

/**
 * Gmail: the mailbox, whether it is armed, the reply check, and a campaign send batch
 * while it runs, as the server rolled them up.
 */
export function gmailStatus(reads: ChipReads, now: number): SystemStatus {
  if (reads.serverDown) return unknown('gmail', 'Gmail', { to: '/gmail' })
  if (reads.status.state !== 'ok') {
    return rollUp('gmail', 'Gmail', [unread('mailbox', 'Mailbox', reads.status)], {
      lead: [],
      fact: '',
      target: { to: '/gmail' },
    })
  }
  return fromServer(reads.status.data.gmail, now)
}

/**
 * LinkedIn: the session, the running run, scheduled runs, the inbox check, prefills,
 * heat, a breaker and a held auto-send, as the server rolled them up.
 */
export function linkedinStatus(reads: ChipReads, now: number): SystemStatus {
  if (reads.serverDown) return unknown('linkedin', 'LinkedIn', { to: '/linkedin' })
  if (reads.status.state !== 'ok') {
    const session = unread('session', 'Session', reads.status, { to: '/linkedin', hash: 'session' })
    return rollUp('linkedin', 'LinkedIn', [session], {
      lead: [],
      fact: '',
      target: { to: '/linkedin' },
    })
  }
  return fromServer(reads.status.data.linkedin, now)
}

// --- netkeep --------------------------------------------------------------------------

export interface NetkeepReads {
  health: Read<{ version: string }>
  stream: EventStreamStatus
  /** `GET /system-status`, for the server's parts: background checks and the Keychain. */
  status: Read<ServerStatus>
}

/** How often the health check runs, so "retrying" can say when. */
export const HEALTH_EVERY_S = 15

/** The server's `NETKEEP_LEAD` and `NETKEEP_CALM` (`netkeep/services/system_status.py`). */
const NETKEEP_LEAD = ['server', 'keychain', 'checks', 'live'] as const
const NETKEEP_CALM = 'server'

/**
 * netkeep itself: the server (reachable, with its version) and live updates (the event
 * stream), which only the UI knows, then the server's own parts: background checks
 * (`netkeep serve`), and the Keychain when it refused the key. Whether the server is
 * down is this chip's to say; the other two read "Unknown".
 */
export function netkeepStatus(reads: NetkeepReads, now: number): SystemStatus {
  const parts: Part[] = []
  const health = reads.health
  const down = health.state === 'error'
  if (health.state === 'pending') {
    parts.push(unread('server', 'Server', health))
  } else if (health.state === 'error') {
    parts.push({
      key: 'server',
      label: 'Server',
      tone: 'bad',
      word: 'Unreachable',
      fact: 'retrying',
      detail: `Can’t reach netkeep · retrying every ${HEALTH_EVERY_S} s`,
    })
  } else {
    parts.push({
      key: 'server',
      label: 'Server',
      tone: 'ok',
      word: 'Connected',
      detail: `Reachable · ${health.data.version}`,
    })
  }
  const live = reads.stream === 'connected'
  parts.push(
    live
      ? {
          key: 'live',
          label: 'Live updates',
          tone: 'ok',
          word: 'Connected',
          detail: 'Connected: pages update as things happen',
        }
      : {
          key: 'live',
          label: 'Live updates',
          tone: 'off',
          word: 'Not live',
          fact: 'reconnecting',
          detail: down
            ? 'Disconnected'
            : 'Disconnected · reconnecting; until then pages refresh on their own timers',
        },
  )
  if (!down && reads.status.state !== 'ok') {
    parts.push(unread('checks', 'Background checks', reads.status))
  } else if (!down && reads.status.state === 'ok') {
    parts.push(...serverParts(reads.status.data.netkeep, now))
  }
  const status = rollUp('netkeep', 'netkeep', parts, {
    lead: NETKEEP_LEAD,
    calm: NETKEEP_CALM,
    fact: live ? 'live' : 'not live',
    target: SUPPORT,
  })
  // Settings can't load without the server; the health check retries on its own.
  return down ? { ...status, reload: true } : status
}

/** "Updated just now", "Updated 3 min ago"; before any read answers, "Not updated yet". */
export function updatedText(at: number, now: number): string {
  return at === 0 ? 'Not updated yet' : `Updated ${ago(new Date(at).toISOString(), now)}`
}
