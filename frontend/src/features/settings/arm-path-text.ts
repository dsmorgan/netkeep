/** The words for the way to arming a mailbox to send (#510). */
import type { Mailbox, MailboxArmSteps } from '@/features/mailboxes/api'

function when(value: string): string {
  return new Date(value).toLocaleString()
}

/** Why netkeeper hasn't found one of its own drafts here yet, in a sentence. */
export function waitingText(steps: MailboxArmSteps): string {
  switch (steps.waiting) {
    case 'not_armed':
      return 'Waits until you arm the mailbox for drafts. netkeeper checks only an armed mailbox.'
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
  if (steps.can_arm_send) return 'Ready. Use Arm to send.'
  return 'Not yet: waits for the steps above.'
}

/** Why Arm to send is off, when it is; null when it can be pressed or isn't shown. */
export function armSendBlocked(mailbox: Mailbox, steps: MailboxArmSteps): string | null {
  if (mailbox.arm !== 'draft' || steps.can_arm_send) return null
  return 'Arm to send is off until netkeeper finds one of its own drafts in this mailbox.'
}
