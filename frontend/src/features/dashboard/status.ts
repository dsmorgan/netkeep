/**
 * The dashboard's status chips and the problems under Needs you (#739), as pure
 * functions of the reads they come from, so every state is tested without rendering.
 *
 * Each chip says one thing in a few words and links to the page or section that fixes
 * it. They replace the Browser health, Mailbox and Budget/heat cards and read the same
 * queries those cards did; a read still in flight or failed says so, never a guess.
 * A problem is something that stopped and waits for you: it shows as a chip's state
 * and again, with its fix, at the top of Needs you.
 */
import type { Attention } from '@/features/attention/api'
import type { BudgetStatus, Heat, LinkedInStatus, Run, Schedule } from '@/features/linkedin/types'
import { runKindLabel } from '@/features/linkedin/types'
import { reasonText, type Mailbox } from '@/features/mailboxes/api'

/** A chip's state color: the B5 state tones, plus none for a plain fact. */
export type ChipTone = 'ok' | 'warn' | 'bad' | 'accent' | 'none'

/** Where a chip or a problem sends you: a page, and the section on it. */
export interface Target {
  to: '/' | '/gmail' | '/linkedin' | '/settings'
  hash?: string
}

export interface Chip extends Target {
  key: 'gmail' | 'session' | 'running' | 'schedule' | 'prefills' | 'problems'
  label: string
  tone: ChipTone
}

export interface Problem extends Target {
  key: 'session' | 'mailbox' | 'heat' | 'breaker'
  title: string
  detail: string
  /** The fix's link text. */
  cta: string
  /** The mailbox to re-authorize, for the mailbox problem. */
  mailboxId?: number
}

/** One query's answer as the chips need it: still in flight, failed, or its data. */
export type Read<T> = { state: 'pending' } | { state: 'error' } | { state: 'ok'; data: T }

export interface StatusReads {
  mailboxes: Read<Mailbox[]>
  status: Read<LinkedInStatus>
  /** The newest run, to name the running one. */
  lastRun: Run | undefined
  schedule: Read<Schedule>
  budget: Read<BudgetStatus>
  heat: Read<Heat>
  attention: Attention | undefined
}

export const GMAIL_SETTINGS: Target = { to: '/settings', hash: 'gmail' }
export const SESSION: Target = { to: '/linkedin', hash: 'session' }
export const NEEDS_YOU_ID = 'needs-you'

/**
 * Which mailbox the chip reads: the one live account (`ok` or `reauth_required`) when
 * there is one, so a person acting on it sees it first; otherwise the most recently
 * touched `disabled` row, so a mailbox someone disconnected still reads as
 * "disconnected" rather than "not connected". An empty list is the only case with none.
 */
export function pickMailbox(mailboxes: Mailbox[]): Mailbox | undefined {
  const live = mailboxes.find((mailbox) => mailbox.status !== 'disabled')
  if (live !== undefined) return live
  return [...mailboxes].sort((a, b) => b.updated_at.localeCompare(a.updated_at))[0]
}

/**
 * What stopped and waits for you, most urgent first: a stopped LinkedIn session, a
 * mailbox Google signed out, heat over its threshold, a tripped breaker. A read that
 * has not answered adds nothing; the chip for it says it is checking.
 */
export function buildProblems(reads: StatusReads): Problem[] {
  const problems: Problem[] = []
  const status = reads.status.state === 'ok' ? reads.status.data : undefined
  if (status !== undefined && status.session_flag !== null) {
    const checkpoint = status.session_flag === 'checkpoint'
    problems.push({
      key: 'session',
      ...SESSION,
      title: checkpoint ? 'LinkedIn asked for a checkpoint' : 'Logged out of LinkedIn',
      detail: checkpoint
        ? 'No run touches the browser until the checkpoint is resolved and the flag cleared.'
        : 'No run touches the browser until you log in and run netkeep preflight.',
      cta: 'See the fix',
    })
  }
  const mailbox = reads.mailboxes.state === 'ok' ? pickMailbox(reads.mailboxes.data) : undefined
  if (mailbox?.status === 'reauth_required') {
    problems.push({
      key: 'mailbox',
      ...GMAIL_SETTINGS,
      title: 'Gmail needs re-authorizing',
      detail: [
        `Nothing is drafted or sent from ${mailbox.email} until you authorize it again.`,
        reasonText(mailbox.status_reason),
      ]
        .filter((part): part is string => part !== null)
        .join(' '),
      cta: 'Settings',
      mailboxId: mailbox.id,
    })
  }
  const heat = reads.heat.state === 'ok' ? reads.heat.data : undefined
  if (heat?.tripped === true) {
    problems.push({
      key: 'heat',
      to: '/linkedin',
      hash: 'heat',
      title: 'LinkedIn heat is over its threshold',
      detail: 'Scheduled LinkedIn runs are skipped until it cools.',
      cta: 'See heat',
    })
  }
  const breaker = reads.attention?.flags.settings ?? null
  if (breaker !== null) {
    problems.push({
      key: 'breaker',
      to: '/settings',
      hash: 'posture',
      title: 'A breaker tripped',
      detail: breaker,
      cta: 'See the breakers',
    })
  }
  return problems
}

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`
}

function gmailChip(read: Read<Mailbox[]>): Chip {
  const chip = (label: string, tone: ChipTone, target: Target = GMAIL_SETTINGS): Chip => ({
    key: 'gmail',
    label,
    tone,
    ...target,
  })
  if (read.state === 'pending') return chip('Gmail: checking…', 'none')
  if (read.state === 'error') return chip('Gmail: couldn’t check', 'none')
  const mailbox = pickMailbox(read.data)
  if (mailbox === undefined) return chip('Gmail not connected', 'none')
  if (mailbox.status === 'reauth_required') return chip('Gmail needs re-authorizing', 'bad')
  if (mailbox.status === 'disabled') return chip('Gmail disconnected', 'none')
  if (mailbox.arm === 'send') return chip('Gmail sending', 'ok', { to: '/gmail' })
  if (mailbox.arm === 'draft') return chip('Gmail drafts only', 'ok', { to: '/gmail' })
  return chip('Gmail not armed', 'warn')
}

/**
 * "No flag" is not "the session is healthy": nothing here checks the live session (a
 * request handler never touches the browser), so the chip says only what it knows.
 */
function sessionChip(read: Read<LinkedInStatus>): Chip {
  const chip = (label: string, tone: ChipTone): Chip => ({
    key: 'session',
    label,
    tone,
    ...SESSION,
  })
  if (read.state === 'pending') return chip('LinkedIn: checking…', 'none')
  if (read.state === 'error') return chip('LinkedIn: couldn’t check', 'none')
  const flag = read.data.session_flag
  if (flag === 'checkpoint') return chip('LinkedIn checkpoint', 'bad')
  if (flag !== null) return chip('LinkedIn logged out', 'bad')
  return chip('No LinkedIn session flag', 'none')
}

function runningChip(read: Read<LinkedInStatus>, lastRun: Run | undefined): Chip | null {
  if (read.state !== 'ok' || read.data.running_run_id === null) return null
  const run = lastRun?.id === read.data.running_run_id ? lastRun : undefined
  return {
    key: 'running',
    label: run === undefined ? 'A LinkedIn run is running' : `${runKindLabel(run)} running`,
    tone: 'accent',
    to: '/linkedin',
    hash: 'runs',
  }
}

function scheduleChip(read: Read<Schedule>): Chip {
  const chip = (label: string, tone: ChipTone): Chip => ({
    key: 'schedule',
    label,
    tone,
    to: '/linkedin',
    hash: 'schedule',
  })
  if (read.state === 'pending') return chip('Scheduled runs: checking…', 'none')
  if (read.state === 'error') return chip('Scheduled runs: couldn’t check', 'none')
  if (read.data.paused) return chip('Scheduled runs paused', 'warn')
  if (!read.data.armed) return chip('Scheduled runs off', 'none')
  if (!read.data.scheduler_running) return chip('No scheduler running', 'bad')
  return chip('Scheduled runs armed', 'ok')
}

/** Today's LinkedIn prefills left, from the `li_prefills` budget's day counters. */
function prefillsChip(read: Read<BudgetStatus>): Chip {
  const chip = (label: string, tone: ChipTone): Chip => ({
    key: 'prefills',
    label,
    tone,
    to: '/settings',
    hash: 'linkedin-budget',
  })
  if (read.state === 'pending') return chip('Prefills: checking…', 'none')
  if (read.state === 'error') return chip('Prefills: couldn’t check', 'none')
  const day = read.data.budgets.find((row) => row.action === 'li_prefills')?.day
  if (day === undefined) return chip('Prefills left: unknown', 'none')
  const left = Math.max(day.remaining, 0)
  return chip(`${left} of ${day.limit} prefills left`, left === 0 ? 'warn' : 'none')
}

/**
 * The chips, in order: Gmail, the LinkedIn session, the running job (only while one
 * runs), scheduled runs, prefills left today, and problems (only above zero).
 */
export function buildChips(reads: StatusReads, problems: Problem[]): Chip[] {
  const chips: (Chip | null)[] = [
    gmailChip(reads.mailboxes),
    sessionChip(reads.status),
    runningChip(reads.status, reads.lastRun),
    scheduleChip(reads.schedule),
    prefillsChip(reads.budget),
    problems.length === 0
      ? null
      : {
          key: 'problems',
          label: plural(problems.length, 'problem', 'problems'),
          tone: 'bad',
          to: '/',
          hash: NEEDS_YOU_ID,
        },
  ]
  return chips.filter((chip): chip is Chip => chip !== null)
}

/**
 * How many things need you: each problem, every LinkedIn step waiting for you or for
 * Try again, a held auto-send, and the unhandled Inbox items. Null until `/attention`
 * answers, since most of the count is its.
 */
export function needsYouCount(
  problems: Problem[],
  attention: Attention | undefined,
): number | null {
  if (attention === undefined) return null
  const held = attention.flags.dashboard === null ? 0 : 1
  return problems.length + attention.counts.dashboard + held + attention.counts.inbox
}

/** The header's one line about what needs you. */
export function needsYouText(count: number | null): string {
  if (count === null) return 'Checking what needs you…'
  if (count === 0) return 'Nothing needs you right now.'
  return `${plural(count, 'thing needs', 'things need')} you.`
}
