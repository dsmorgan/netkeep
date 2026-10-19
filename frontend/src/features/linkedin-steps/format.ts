/**
 * Words for the LinkedIn step screens (#383): why a prefill was refused, and how
 * long a prefilled message has waited.
 */
import { formatWhen } from '@/features/campaigns/format'
import type { Schedule } from '@/features/linkedin/types'
import { WHY_IT_MATTERS, type LintRule } from '@/features/templates/lint'

import type { WaitingItem } from './api'

export const ONE_AT_A_TIME = 'One prefill at a time: send or discard the open one first.'
export { TYPING } from './prefill-copy'
export const REVIEW_AND_SEND = 'Review it in Chrome and click Send yourself.'

/**
 * An `auto_send` step in the queue, with auto-send on or off (Settings, or config.toml). With it on,
 * the row says when the step goes ({@link autoSendStepText}), then this.
 */
export const PREFILL_IT_YOURSELF = 'You can still prefill it yourself.'
export const AUTO_SEND_STEP_OFF =
  'Auto-send step, but auto-send is off in Settings, so it waits for you to prefill it.'

/** A Prefill step in the queue: nothing happens until you prefill it. */
export const WAITS_FOR_PREFILL = 'Waits for you to prefill.'

/** The queue's auto-send banner: only steps set to Auto-send send on their own. */
export const AUTO_SEND_ON_NOTE =
  'netkeep sends LinkedIn messages itself for steps set to Auto-send. Steps set to Prefill still wait for you. See Settings, Posture.'

/** A due step your sending hours hold, as a sentence. */
export function heldText(until: string): string {
  return `Your sending hours hold it until ${formatWhen(until)}.`
}

/** A stale message auto-send clicked Send for (ADR 0008). */
export const AUTO_SENT_STALE =
  'Auto-sent three days ago or more, and the inbox poll never saw it sent. Check the conversation in Chrome: if it went, check now; if not, discard it.'

/** A prefilled message this old is stale (spec 11.6): it no longer holds the one slot. */
export const STALE_AFTER_DAYS = 3

/**
 * Each refusal reason the claim can give, in plain words. The claim's own
 * (`Refusal`), the engine's skips, and the guards' reasons
 * (`netkeep/services/linkedin_steps.py`, `campaign_engine.py`, `campaign_guards.py`).
 * A reason not here is shown as the backend sent it.
 */
export const REASON_TEXT: Readonly<Record<string, string>> = {
  run_refused: "netkeep couldn't start the prefill run, so nothing was typed in Chrome",
  linkedin_inbox_stale:
    "held until the inbox is read: netkeep hasn't read your LinkedIn inbox recently enough to know about replies",
  run_in_progress: 'another LinkedIn run is going; wait for it to finish',
  prefill_open: 'another prefill is open: send or discard it first',
  outside_active_hours: "it's outside your LinkedIn active hours",
  auto_send_off: 'auto-send is off in Settings',
  not_an_auto_send_step: "this step isn't an auto-send step",
  bad_active_hours: "your LinkedIn active hours can't be read; fix them in config.toml",
  not_a_linkedin_step: "this enrollment's next step isn't a LinkedIn step",
  no_step: 'this enrollment has no next step',
  rendered_errors: "the message for this contact has errors, so it's parked for you to fix",
  browser_unknown: 'nothing says the LinkedIn session is logged in; run a preflight first',
  browser_unhealthy: 'the LinkedIn session is flagged or heat is too high',
  browser_out_of_budget: "today's LinkedIn prefill budget is spent",
  replied: 'the contact replied, so the enrollment ended',
  ended: 'the enrollment ended',
  step_already_sent: 'this step already has a message',
  waiting_on_unsent: "an earlier message on this enrollment hasn't gone out yet",
  not_due: "this step isn't due yet",
  not_started: "the campaign hasn't started yet",
  outside_sending_hours: "it's outside your sending hours",
  spilled_to_next_day: 'your sending hours hold it until the next day',
  bad_schedule: "your sending hours can't be read; fix them in Settings",
  spacing: "the step's delay since the last step hasn't passed",
  template_errors: 'the template has lint errors',
  render_failed: "the message didn't render for this contact",
  guard_excluded: 'the guards leave this contact out',
  campaign_not_active: "the campaign isn't active",
  enrollment_not_active: "the enrollment isn't active",
  no_linkedin: 'the contact has no LinkedIn identity',
  do_not_contact: 'the contact is marked do not contact',
  archived: 'the contact is archived',
  merged: 'the contact was merged into another',
  self: "that's you",
  needs_review: 'the contact needs review first',
  contacted_recently: 'you contacted them recently',
  disconnected: "you're no longer connected to this contact on LinkedIn",
  campaign_at_cap: "the campaign has reached today's cap",
  campaign_blocked: 'the campaign is blocked, for example because it has no steps',
  in_another_campaign: "they're in another active campaign",
  try_again_needed: 'the last prefill typed nothing; use Try again',
  nothing_to_retry:
    'only a step whose last prefill typed nothing can be tried again; a part-typed one must be cleared and discarded first, and one too long waits for the template to change',
  confirm_no_bubble:
    'the last try clicked Message; confirm that no message bubble for this contact is open in Chrome',
}

/** One reason as words: the table above, a lint rule's own reason, or the code itself. */
export function reasonText(reason: string): string {
  const text = REASON_TEXT[reason]
  if (text !== undefined) return text
  if (reason in WHY_IT_MATTERS) {
    return `${reason.replace(/_/g, ' ')}: ${WHY_IT_MATTERS[reason as LintRule]}`
  }
  return reason.replace(/_/g, ' ')
}

/** Whole days between `iso` and `now`, never below zero. */
export function daysSince(iso: string, now: Date): number {
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return 0
  return Math.max(0, Math.floor((now.getTime() - then) / 86_400_000))
}

/** "prefilled 2 hours ago", "prefilled 3 days ago"; "prefilled" when the time is unknown. */
export function ageText(iso: string | null, now: Date): string {
  if (iso === null) return 'not recorded'
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return iso
  const minutes = Math.max(0, Math.round((now.getTime() - then) / 60_000))
  const format = new Intl.RelativeTimeFormat('en', { numeric: 'auto' })
  if (minutes < 60) return format.format(-minutes, 'minute')
  const hours = Math.floor(minutes / 60)
  if (hours < 48) return format.format(-hours, 'hour')
  return format.format(-Math.floor(hours / 24), 'day')
}

/** What a waiting message needs from you: send it, a stale one, a stopped prefill, or a partly typed one. */
export type WaitingState = 'prefilled' | 'stale' | 'interrupted' | 'partly_typed'

export function waitingState(item: WaitingItem, now: Date): WaitingState {
  if (item.partly_typed) return 'partly_typed'
  if (item.interrupted) return 'interrupted'
  if (item.status === 'stale') return 'stale'
  // The tick marks it `stale` within a minute; until then the age says it already is.
  if (item.prefilled_at !== null && daysSince(item.prefilled_at, now) >= STALE_AFTER_DAYS) {
    return 'stale'
  }
  return 'prefilled'
}

/**
 * The open prefill, if any: one typed and not stale yet, or one whose prefill stopped
 * before it said what it typed. Either holds the one slot (spec 11.6).
 */
export function openPrefill(items: readonly WaitingItem[], now: Date): WaitingItem | null {
  return items.find((item) => waitingState(item, now) !== 'stale') ?? null
}

/**
 * `https://www.linkedin.com/messaging/thread/<id>/` from a stored conversation URN:
 * `urn:li:msg_conversation:<id>`, or `urn:li:msg_conversation:(<profile urn>,<id>)`.
 * Null for anything else, so a malformed value never becomes a link.
 */
export function threadUrl(urn: string | null): string | null {
  if (urn === null) return null
  const match = /^urn:li:[a-z_]*conversation:(?:\([^,()]+,([^,()]+)\)|([^,():]+))$/i.exec(urn)
  const id = match?.[1] ?? match?.[2]
  if (id === undefined || id === '') return null
  return `https://www.linkedin.com/messaging/thread/${encodeURIComponent(id)}/`
}

// --- when an auto-send step goes out, or why it waits ---------------------------------------

/** The next auto-send check, from `GET /linkedin/schedule` (`auto_send`). */
export type AutoSendOutlook = NonNullable<Schedule['auto_send']>

/**
 * Why the next auto-send check would send nothing, in plain words: one sentence, or its
 * start when the time it ends follows. The backend's `Wait` codes
 * (`netkeep/services/auto_send_outlook.py`); a code not here is shown spaced out.
 */
export const AUTO_SEND_WAIT_TEXT: Readonly<Record<string, string>> = {
  auto_send_off: 'Auto-send is off in Settings.',
  not_scheduled:
    "The auto-send check isn't scheduled yet. netkeep serve schedules it when it starts.",
  disarmed: 'Scheduled runs are off, so the auto-send check sends nothing.',
  paused: 'Scheduled runs are paused, so the auto-send check sends nothing.',
  session_flagged:
    "The LinkedIn session is flagged (a checkpoint or a login wall). Nothing runs until it's cleared.",
  heat: 'Heat is at or over its threshold',
  held: 'Auto-send is held',
  spacing: 'It waits out the spacing after the last LinkedIn message run',
  budget_spent: "Today's auto-send budget is spent",
  no_step_due: 'No auto-send step is due.',
  bad_sending_hours: "Your sending hours can't be read. Fix them in Settings.",
  sending_hours: 'Your sending hours hold every due auto-send step',
  spilled:
    'Your sending hours are any time, so each due auto-send step waits for its own time of day',
  prefill_open: 'Another prefill is open. Send or discard it first.',
  outside_active_hours: "It's outside your LinkedIn active hours",
  bad_active_hours: "Your LinkedIn active hours can't be read. Fix them in config.toml.",
  not_logged_in: 'Nothing says the LinkedIn session is logged in. Run a preflight first.',
  browser_unhealthy: 'The LinkedIn session is flagged or heat is too high.',
  out_of_budget: "Today's LinkedIn budget for messages or profile visits is spent",
  inbox_stale: "The LinkedIn inbox hasn't been read recently",
}

/** How the time a wait ends reads after its words. */
const UNTIL_TEXT: Readonly<Record<string, string>> = {
  heat: 'It cools by',
  budget_spent: 'It resets at',
  out_of_budget: 'It resets at',
  sending_hours: 'They open at',
  spilled: 'The first goes at',
  outside_active_hours: 'They next open at',
}

function hours(value: number): string {
  return `${value.toFixed(1)} hours`
}

/**
 * Why the next auto-send check would send nothing, with the time the wait ends where
 * the check knows it. Times are local, in the page's format. `null` when it would send.
 */
export function autoSendWaitText(outlook: AutoSendOutlook): string | null {
  const wait = outlook.wait
  if (wait === null) return null
  const words = AUTO_SEND_WAIT_TEXT[wait] ?? `${wait.replace(/_/g, ' ')}.`
  if (wait === 'held') {
    return outlook.hold_reason === null ? `${words}.` : `${words}: ${outlook.hold_reason}.`
  }
  if (wait === 'spacing' && outlook.until !== null) {
    const bounds =
      outlook.spacing_earliest !== null && outlook.spacing_latest !== null
        ? ` The spacing is ${outlook.spacing_min_minutes} to ${outlook.spacing_max_minutes} minutes, so it ends between ${formatWhen(outlook.spacing_earliest)} and ${formatWhen(outlook.spacing_latest)}.`
        : ''
    return `${words}. It ends at ${formatWhen(outlook.until)}.${bounds}`
  }
  if (wait === 'inbox_stale') {
    if (outlook.inbox_age_hours === null) return `${words}: no inbox poll has completed yet.`
    const limit =
      outlook.inbox_limit_hours === null
        ? ''
        : `, and the limit is ${hours(outlook.inbox_limit_hours)}`
    return `${words}: the last complete poll was ${hours(outlook.inbox_age_hours)} ago${limit}.`
  }
  if (words.endsWith('.')) return words
  if (outlook.until !== null) {
    return `${words}. ${UNTIL_TEXT[wait] ?? 'Until'} ${formatWhen(outlook.until)}.`
  }
  return `${words}.`
}

/**
 * One auto-send step: when it goes out on its own, or why it waits. `due` is the step's
 * due time, and `now` the time to compare it with. Auto-send must be on.
 */
export function autoSendStepText(
  outlook: AutoSendOutlook | null | undefined,
  step: { enrollment_id: number; due: string },
  now: Date,
): string {
  if (outlook === null || outlook === undefined) return 'Sends on its own.'
  if (outlook.wait === null) {
    if (outlook.enrollment_id === step.enrollment_id) {
      return `Sends on its own at the next auto-send check, ${autoSendCheckWhen(outlook)}.`
    }
    return 'Sends on its own after the auto-send steps due before it, one at a time.'
  }
  if (new Date(step.due).getTime() > now.getTime()) {
    return 'Sends on its own once it is due.'
  }
  return `Sends on its own, but waits. ${autoSendWaitText(outlook)}`
}

/**
 * When the next auto-send check runs: its time, or, when it is overdue a whole interval,
 * the window the scheduler runs its one catch-up in.
 */
export function autoSendCheckWhen(outlook: AutoSendOutlook): string {
  if (outlook.catch_up && outlook.catch_up_latest !== null) {
    return `between ${formatWhen(outlook.checked_at)} and ${formatWhen(outlook.catch_up_latest)}`
  }
  return formatWhen(outlook.checked_at)
}
