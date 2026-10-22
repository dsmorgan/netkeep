/**
 * The problems under Needs you (#739) and how many things need you, as pure functions
 * of the reads they come from, so every state is tested without rendering.
 *
 * A problem is something that stopped and waits for you: it shows as a status chip's
 * state in the top bar (#751) and again, with its fix, at the top of Needs you.
 */
import type { Attention } from '@/features/attention/api'
import { formatWhen } from '@/features/linkedin/fields'
import type { Heat, LinkedInStatus } from '@/features/linkedin/types'
import { reasonText, type Mailbox } from '@/features/mailboxes/api'
import { GMAIL_SETTINGS, type Read, type Target } from '@/features/system-status/roll-up'

export interface Problem extends Target {
  key: 'session' | 'mailbox' | 'heat' | 'breaker'
  title: string
  detail: string
  /** The fix's link text. */
  cta: string
  /** The mailbox to re-authorize, for the mailbox problem. */
  mailboxId?: number
}

/** The reads the problems come from. */
export interface StatusReads {
  mailboxes: Read<Mailbox[]>
  status: Read<LinkedInStatus>
  heat: Read<Heat>
  attention: Attention | undefined
}

/**
 * Which mailbox the problems read: the one live account (`ok` or `reauth_required`)
 * when there is one, so a person acting on it sees it first; otherwise the most
 * recently touched `disabled` row. An empty list is the only case with none. The Gmail
 * chip picks the same way, on the server (`pick_mailbox`, #761).
 */
export function pickMailbox(mailboxes: Mailbox[]): Mailbox | undefined {
  const live = mailboxes.find((mailbox) => mailbox.status !== 'disabled')
  if (live !== undefined) return live
  return [...mailboxes].sort((a, b) => b.updated_at.localeCompare(a.updated_at))[0]
}

export const SESSION: Target = { to: '/linkedin', hash: 'session' }
export const NEEDS_YOU_ID = 'needs-you'

/**
 * What stopped and waits for you, most urgent first: a stopped LinkedIn session, a
 * mailbox Google signed out, heat over its threshold, a tripped breaker. A read that
 * has not answered adds nothing; the top bar's chip for it says it is checking.
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
      detail:
        heat.resumes_at === null
          ? 'Scheduled LinkedIn runs are skipped until it cools.'
          : `Scheduled LinkedIn runs are skipped until ${formatWhen(heat.resumes_at)}.`,
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
