/**
 * The top bar's three status chips (#751), Gmail, LinkedIn and netkeep, as pure
 * functions of the reads they come from, so every state is tested without rendering.
 *
 * Each chip rolls up its parts: every part has a tone on one scale, the chip takes the
 * worst, and its word and fact come from the part that set it. A read still in flight
 * or failed is a part of its own that says so, never a guess. When the server can't be
 * reached, Gmail and LinkedIn say "Unknown" rather than their last answer.
 */
import type { UseQueryResult } from '@tanstack/react-query'

import type { Attention } from '@/features/attention/api'
import type { EventStreamStatus } from '@/features/events/use-event-stream'
import { formatWhen } from '@/features/linkedin/fields'
import type {
  BudgetStatus,
  Heat,
  LinkedInStatus,
  Run,
  RunKind,
  Schedule,
} from '@/features/linkedin/types'
import { runKindLabel } from '@/features/linkedin/types'
import { reasonText, type Mailbox } from '@/features/mailboxes/api'
import type { CheckState, PollCheck, PollStatus } from '@/features/poll-status/api'
import { STATE_TEXT, ago, checkSummary, nextText } from '@/features/poll-status/format'

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
function unread(key: string, label: string, read: { state: 'pending' | 'error' }): Part {
  const pending = read.state === 'pending'
  return {
    key,
    label,
    tone: 'off',
    word: pending ? 'Checking…' : 'Couldn’t check',
    fact: '',
    detail: pending ? 'Checking…' : 'Couldn’t be read; netkeep tries again shortly',
  }
}

function capitalized(text: string): string {
  return text === '' ? text : text.charAt(0).toUpperCase() + text.slice(1)
}

/**
 * A background check's tone. A check that waits on its own (outside active hours,
 * nothing to look at) is working as set up. One that `netkeep serve` doesn't run is
 * off here; the netkeep chip is the one that says serve isn't running.
 */
export const CHECK_TONE: Record<CheckState, StatusTone> = {
  scheduled: 'ok',
  due: 'ok',
  idle: 'ok',
  outside_hours: 'ok',
  paused: 'warn',
  blocked: 'warn',
  off: 'off',
  not_running: 'off',
  not_wired: 'off',
}

/** "checked 3 min ago", or "not checked yet". */
function lastChecked(check: PollCheck, now: number): string {
  return check.last_at ? `checked ${ago(check.last_at, now)}` : 'not checked yet'
}

function checkPart(
  key: string,
  label: string,
  check: PollCheck,
  now: number,
  fact: string,
  target?: Target,
): Part {
  const summary = capitalized(checkSummary(check, now))
  return {
    key,
    label,
    tone: CHECK_TONE[check.state],
    word: check.state === 'scheduled' ? 'Checked' : capitalized(STATE_TEXT[check.state]),
    fact,
    detail: check.reason ? `${summary}. ${check.reason}` : summary,
    target,
  }
}

function findCheck(poll: Read<PollStatus>, key: string): Read<PollCheck | undefined> {
  if (poll.state !== 'ok') return poll
  return { state: 'ok', data: poll.data.items.find((check) => check.key === key) }
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

// --- Gmail ----------------------------------------------------------------------------

/**
 * Which mailbox Gmail reads: the one live account (`ok` or `reauth_required`) when
 * there is one, so a person acting on it sees it first; otherwise the most recently
 * touched `disabled` row, so a mailbox someone disconnected still reads as
 * "disconnected" rather than "not connected". An empty list is the only case with none.
 */
export function pickMailbox(mailboxes: Mailbox[]): Mailbox | undefined {
  const live = mailboxes.find((mailbox) => mailbox.status !== 'disabled')
  if (live !== undefined) return live
  return [...mailboxes].sort((a, b) => b.updated_at.localeCompare(a.updated_at))[0]
}

export interface GmailReads {
  /** The mailbox list, never `/mailboxes/status`, so it answers while the Keychain is locked. */
  mailboxes: Read<Mailbox[]>
  poll: Read<PollStatus>
  /** A campaign send batch just ran (`campaign.tick`, #547). */
  sending: boolean
  serverDown: boolean
}

function mailboxPart(read: Read<Mailbox[]>): Part {
  if (read.state !== 'ok') return unread('mailbox', 'Mailbox', read)
  const mailbox = pickMailbox(read.data)
  const part = { key: 'mailbox', label: 'Mailbox', target: GMAIL_SETTINGS }
  if (mailbox === undefined) {
    return {
      ...part,
      tone: 'off',
      word: 'Not set up',
      fact: '',
      detail: 'No Gmail account is connected',
    }
  }
  if (mailbox.status === 'disabled') {
    return {
      ...part,
      tone: 'off',
      word: 'Disconnected',
      fact: '',
      detail: `${mailbox.email} · disconnected`,
    }
  }
  if (mailbox.status === 'reauth_required') {
    return {
      ...part,
      tone: 'bad',
      word: 'Re-authorize',
      fact: 'nothing sent or checked',
      detail: [
        `Google signed netkeep out of ${mailbox.email}. Nothing is drafted, sent or checked until you authorize it again.`,
        reasonText(mailbox.status_reason),
      ]
        .filter((text): text is string => text !== null)
        .join(' '),
    }
  }
  return {
    key: 'mailbox',
    label: 'Mailbox',
    tone: 'ok',
    word: 'Connected',
    detail: `${mailbox.email} · connected`,
  }
}

function sendingPart(mailbox: Mailbox): Part {
  const part = { key: 'sending', label: 'Sending' }
  if (mailbox.arm === 'send') {
    return { ...part, tone: 'ok', word: 'Sending', detail: 'Armed to send' }
  }
  if (mailbox.arm === 'draft') {
    return {
      ...part,
      tone: 'ok',
      word: 'Drafts only',
      detail: 'Armed for drafts only: nothing is sent',
    }
  }
  return {
    ...part,
    tone: 'warn',
    word: 'Not armed',
    detail: 'Not armed: nothing is drafted or sent',
    target: GMAIL_SETTINGS,
  }
}

const BATCH: Part = {
  key: 'batch',
  label: 'Campaign batch',
  tone: 'busy',
  word: 'Sending now',
  fact: 'campaign batch',
  detail: 'A campaign send batch just ran',
}

/**
 * Gmail: the mailbox (connected, needs re-authorizing or disconnected), whether it is
 * armed (drafts only, sending or not armed), the reply check, and a campaign send batch
 * while it runs. All well, it says how it sends and when replies were last checked.
 */
export function gmailStatus(reads: GmailReads, now: number): SystemStatus {
  if (reads.serverDown) return unknown('gmail', 'Gmail', { to: '/gmail' })
  const parts: Part[] = [mailboxPart(reads.mailboxes)]
  const mailbox = reads.mailboxes.state === 'ok' ? pickMailbox(reads.mailboxes.data) : undefined
  if (mailbox?.status === 'ok') parts.push(sendingPart(mailbox))
  if (reads.sending) parts.push(BATCH)
  let fact = ''
  const replies = findCheck(reads.poll, 'gmail_replies')
  if (replies.state !== 'ok') {
    parts.push(unread('replies', 'Reply check', replies))
  } else if (replies.data !== undefined) {
    fact = lastChecked(replies.data, now)
    parts.push(checkPart('replies', 'Reply check', replies.data, now, fact))
  }
  return rollUp('gmail', 'Gmail', parts, {
    lead: ['mailbox', 'batch', 'sending', 'replies'],
    calm: 'sending',
    fact,
    target: { to: '/gmail' },
  })
}

// --- LinkedIn -------------------------------------------------------------------------

export interface LinkedInReads {
  status: Read<LinkedInStatus>
  /** The newest run, to name the running one. */
  lastRun: Run | undefined
  /** The latest `run.progress` event, so the running run's count moves without a refetch. */
  progress?: RunProgress | null
  schedule: Read<Schedule>
  budget: Read<BudgetStatus>
  heat: Read<Heat>
  poll: Read<PollStatus>
  attention: Attention | undefined
  serverDown: boolean
}

const RUN_WORDS: Record<RunKind, string> = {
  connections_full: 'Full sync',
  connections_incremental: 'Sync',
  enrich: 'Enrichment',
  inbox: 'Inbox check',
  message_send: 'Prefill',
  session_check: 'Connection check',
}

const linkedin = (hash: string): Target => ({ to: '/linkedin', hash })

/**
 * "No flag" is not "the session is healthy": nothing here checks the live session (a
 * request handler never touches the browser), so the part says only what it knows.
 */
function sessionPart(read: Read<LinkedInStatus>): Part {
  const target = linkedin('session')
  if (read.state !== 'ok') return { ...unread('session', 'Session', read), target }
  const flag = read.data.session_flag
  if (flag === 'checkpoint') {
    return {
      key: 'session',
      label: 'Session',
      tone: 'bad',
      word: 'Checkpoint',
      fact: 'runs stopped',
      detail:
        'LinkedIn asked for a checkpoint. No run touches the browser until it is resolved and the flag cleared.',
      target,
    }
  }
  if (flag !== null) {
    return {
      key: 'session',
      label: 'Session',
      tone: 'bad',
      word: 'Logged out',
      fact: 'runs stopped',
      detail: 'Logged out. No run touches the browser until you log in and run netkeep preflight.',
      target,
    }
  }
  return {
    key: 'session',
    label: 'Session',
    tone: 'ok',
    word: 'No session flag',
    detail: 'No session flag: no run has found LinkedIn logged out',
    target,
  }
}

/** A `run.progress` event: the run's id and its progress counts. */
export type RunProgress = { run_id: number } & Record<string, unknown>

function count(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

/**
 * How far a run got, as "340 of 626", from the two known progress shapes (spec 9.10's
 * `ProgressEvent`): an enrichment's `visited` of `planned`, a sync's `connections` of
 * LinkedIn's `total`. Anything else has no count to show.
 */
export function progressText(progress: Record<string, unknown> | null | undefined): string | null {
  if (progress == null) return null
  const pairs: [unknown, unknown][] = [
    [progress.visited, progress.planned],
    [progress.connections, progress.total],
  ]
  for (const [done, of] of pairs) {
    const a = count(done)
    const b = count(of)
    if (a !== null && b !== null && b > 0) return `${a} of ${b}`
  }
  return null
}

function runningPart(
  status: LinkedInStatus,
  lastRun: Run | undefined,
  live: RunProgress | null | undefined,
  now: number,
): Part | null {
  if (status.running_run_id === null) return null
  const run = lastRun?.id === status.running_run_id ? lastRun : undefined
  const target = linkedin('runs')
  if (run === undefined) {
    return {
      key: 'running',
      label: 'Running',
      tone: 'busy',
      word: 'Running',
      fact: '',
      detail: 'A LinkedIn run is running',
      target,
    }
  }
  const word =
    run.kind === 'message_send' && run.trigger === 'scheduled' ? 'Auto-send' : RUN_WORDS[run.kind]
  const progress =
    progressText(live?.run_id === run.id ? live : null) ??
    progressText(run.progress) ??
    (run.planned === null ? null : `${run.completed ?? 0} of ${run.planned}`)
  const started = `started ${ago(run.started_at, now)}`
  return {
    key: 'running',
    label: 'Running',
    tone: 'busy',
    word: `${word} running`,
    fact: progress ?? started,
    detail: [runKindLabel(run), progress, started]
      .filter((text): text is string => text !== null)
      .join(' · '),
    target,
  }
}

function heldPart(attention: Attention | undefined): Part | null {
  const held = attention?.flags.dashboard ?? null
  if (held === null) return null
  // Resume is with the LinkedIn steps, under Needs you on the dashboard.
  return {
    key: 'held',
    label: 'Auto-send',
    tone: 'warn',
    word: 'Auto-send held',
    fact: '',
    detail: held,
    target: { to: '/', hash: 'needs-you' },
  }
}

function schedulePart(read: Read<Schedule>, now: number): Part {
  const target = linkedin('schedule')
  const part = { key: 'schedule', label: 'Scheduled runs', target }
  if (read.state !== 'ok') return { ...unread('schedule', 'Scheduled runs', read), target }
  const schedule = read.data
  if (schedule.paused) {
    return {
      ...part,
      tone: 'warn',
      word: 'Paused',
      detail: 'Paused: no scheduled run starts until you resume',
    }
  }
  if (!schedule.armed) {
    return {
      ...part,
      tone: 'off',
      word: 'Off',
      fact: '',
      detail: 'Off: nothing runs on a schedule',
    }
  }
  if (!schedule.scheduler_running) {
    return {
      ...part,
      tone: 'bad',
      word: 'No scheduler',
      fact: 'runs don’t start',
      detail: 'Armed, but no scheduler is running, so nothing starts on time',
    }
  }
  const next = schedule.jobs
    .map((job) => job.next_due)
    .filter((due): due is string => due !== null)
    .sort()[0]
  return {
    ...part,
    tone: 'ok',
    word: 'Armed',
    detail: next === undefined ? 'Armed' : `Armed · next run ${nextText(next, now)}`,
  }
}

function prefillsPart(read: Read<BudgetStatus>): Part | null {
  const target = linkedin('budget')
  if (read.state !== 'ok') return { ...unread('prefills', 'Prefills today', read), target }
  const day = read.data.budgets.find((row) => row.action === 'li_prefills')?.day
  if (day === undefined) return null
  const left = Math.max(day.remaining, 0)
  const text = `${left} of ${day.limit} left`
  return left === 0
    ? {
        key: 'prefills',
        label: 'Prefills today',
        tone: 'warn',
        word: 'No prefills left',
        fact: 'until tomorrow',
        detail: text,
        target,
      }
    : {
        key: 'prefills',
        label: 'Prefills today',
        tone: 'ok',
        word: `${left} prefills left`,
        detail: text,
        target,
      }
}

function heatPart(read: Read<Heat>): Part {
  const target = linkedin('heat')
  if (read.state !== 'ok') return { ...unread('heat', 'Heat', read), target }
  const heat = read.data
  if (!heat.tripped) {
    return { key: 'heat', label: 'Heat', tone: 'ok', word: 'Heat normal', detail: 'Normal', target }
  }
  return {
    key: 'heat',
    label: 'Heat',
    tone: 'bad',
    word: 'Heat high',
    fact: heat.resumes_at === null ? 'runs wait' : `runs wait until ${formatWhen(heat.resumes_at)}`,
    detail:
      heat.resumes_at === null
        ? 'Over its threshold: scheduled runs are skipped until it cools'
        : `Over its threshold: scheduled runs are skipped until ${formatWhen(heat.resumes_at)}`,
    target,
  }
}

/**
 * LinkedIn: the session (signed in, logged out or a checkpoint), the running run with
 * its progress, scheduled runs (armed, paused, off or no scheduler), the inbox check,
 * prefills left today, heat and a held auto-send. All well, it says scheduled runs are
 * armed and when the inbox was last checked.
 */
export function linkedinStatus(reads: LinkedInReads, now: number): SystemStatus {
  if (reads.serverDown) return unknown('linkedin', 'LinkedIn', { to: '/linkedin' })
  const parts: (Part | null)[] = [sessionPart(reads.status)]
  if (reads.status.state === 'ok')
    parts.push(runningPart(reads.status.data, reads.lastRun, reads.progress, now))
  parts.push(schedulePart(reads.schedule, now))
  let fact = ''
  const inbox = findCheck(reads.poll, 'linkedin_inbox')
  if (inbox.state !== 'ok') {
    parts.push({ ...unread('inbox', 'Inbox check', inbox), target: linkedin('inbox') })
  } else if (inbox.data !== undefined) {
    fact = `inbox ${lastChecked(inbox.data, now).replace(/^checked /, '')}`
    parts.push(checkPart('inbox', 'Inbox check', inbox.data, now, fact, linkedin('inbox')))
  }
  parts.push(prefillsPart(reads.budget), heatPart(reads.heat), heldPart(reads.attention))
  return rollUp(
    'linkedin',
    'LinkedIn',
    parts.filter((part): part is Part => part !== null),
    {
      lead: ['session', 'heat', 'held', 'running', 'schedule', 'inbox', 'prefills'],
      calm: 'schedule',
      fact,
      target: { to: '/linkedin' },
    },
  )
}

// --- netkeep --------------------------------------------------------------------------

export interface NetkeepReads {
  health: Read<{ version: string }>
  stream: EventStreamStatus
  poll: Read<PollStatus>
  /** False when the Keychain refused the local API key (the session check, #618). */
  keyPersisted: boolean
}

/** How often the health check runs, so "retrying" can say when. */
export const HEALTH_EVERY_S = 15

/**
 * netkeep itself: the server (reachable, with its version), live updates (the event
 * stream), background checks (`netkeep serve`), and the Keychain when it refused the
 * key. Whether the server is down is this chip's to say; the other two read "Unknown".
 */
export function netkeepStatus(reads: NetkeepReads): SystemStatus {
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
  if (!down && reads.poll.state === 'ok') {
    parts.push(
      reads.poll.data.background_running
        ? {
            key: 'checks',
            label: 'Background checks',
            tone: 'ok',
            word: 'Connected',
            detail: 'netkeep serve is running them',
          }
        : {
            key: 'checks',
            label: 'Background checks',
            tone: 'warn',
            word: 'No background checks',
            fact: 'serve isn’t running',
            detail: 'netkeep serve isn’t running, so nothing is checked in the background',
          },
    )
  }
  if (!reads.keyPersisted) {
    parts.push({
      key: 'keychain',
      label: 'Keychain',
      tone: 'warn',
      word: 'Keychain refused',
      fact: 'key in memory',
      detail: 'Refused · the local API key lives in memory until netkeep stops',
    })
  }
  return rollUp('netkeep', 'netkeep', parts, {
    lead: ['server', 'keychain', 'checks', 'live'],
    calm: 'server',
    fact: live ? 'live' : 'not live',
    target: SUPPORT,
  })
}

/** "Updated just now", "Updated 3 min ago"; before any read answers, "Not updated yet". */
export function updatedText(at: number, now: number): string {
  return at === 0 ? 'Not updated yet' : `Updated ${ago(new Date(at).toISOString(), now)}`
}
