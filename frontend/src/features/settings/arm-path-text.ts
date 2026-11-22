/**
 * The words for the steps to Gmail Automatic (#510). They name the Gmail switch's
 * positions (#829), the only control that moves a mailbox there (#834).
 */
import type { Mailbox, MailboxArmSteps } from '@/features/mailboxes/api'

/** Where the switch is, said the same way in each step that points at it. */
export const MOVE_TO_MANUAL = 'Move Gmail to Manual under Automation.'
export const MOVE_TO_AUTOMATIC = 'Move Gmail to Automatic under Automation.'

function when(value: string): string {
  return new Date(value).toLocaleString()
}

/** Why netkeep hasn't found one of its own drafts here yet, in a sentence. */
export function waitingText(steps: MailboxArmSteps): string {
  switch (steps.waiting) {
    case 'not_armed':
      return `Waits until Gmail is on Manual: netkeep checks only on Manual or Automatic. ${MOVE_TO_MANUAL}`
    case 'reauth_required':
      return 'Waiting: the mailbox needs you to sign in again. Re-authorize it, and the next check runs.'
    case 'no_draft':
      return 'Waiting: no draft yet. Send a test from a campaign’s review, or let a campaign make its first draft.'
    case 'test_draft_not_found':
      return 'Waiting: the last check didn’t find the test draft in Gmail. If you discarded it, send a new test.'
    case 'not_checked':
      return 'Waiting: not checked yet.'
    case 'not_found':
      return steps.checked_at === null
        ? 'Waiting: not found yet.'
        : `Waiting: not found yet. Last checked ${when(steps.checked_at)}.`
    case 'disconnected':
      return 'The mailbox is disconnected. Connect it again first.'
    case null:
      return steps.verified_at === null ? '' : `Found ${when(steps.verified_at)}.`
  }
}

export function sendText(steps: MailboxArmSteps): string {
  if (steps.armed_to_send) return 'Done.'
  if (steps.can_arm_send) return `Ready. ${MOVE_TO_AUTOMATIC}`
  return 'Not yet: waits for the steps above.'
}

/** Why Automatic is unavailable for a mailbox on Manual; null when it is ready, or not on Manual. */
export function armSendBlocked(mailbox: Mailbox, steps: MailboxArmSteps): string | null {
  if (mailbox.arm !== 'draft' || steps.can_arm_send) return null
  return 'Automatic is unavailable until netkeep finds one of its own drafts in this mailbox.'
}
