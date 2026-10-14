/**
 * What a leftover draft's row says (#543): why netkeeper no longer wants it, and the one
 * note it carries. A warning comes first: sending that draft would ignore an opt-out, a
 * bounce or the do-not-send list, and nothing in netkeeper can stop a send made in Gmail.
 */
import type { LeftoverDraft } from './api'

const ENDED: Record<string, string> = {
  opted_out: 'Opted out',
  bounced: 'Bounced',
  replied: 'Replied',
  removed: 'Removed from the campaign',
  completed: 'Campaign finished for them',
}

/** Why netkeeper no longer wants the draft, in a few words. */
export function leftoverReason(row: LeftoverDraft): string {
  switch (row.kind) {
    case 'discarded':
      return 'Discarded by a contact merge'
    case 'test':
      return 'Test send'
    case 'waiting':
      return 'Still waiting in the campaign'
    default:
      return (row.enrollment_status && ENDED[row.enrollment_status]) ?? 'Enrollment ended'
  }
}

const WARNINGS: Record<string, string> = {
  opted_out: 'They opted out. Don’t send this draft.',
  bounced: 'Their address bounced. Don’t send this draft.',
  do_not_contact: 'They’re marked do not contact. Don’t send this draft.',
  do_not_send: 'An address of theirs is on the do-not-send list. Don’t send this draft.',
}

/** The one note a row carries, if any: a warning first. */
export function leftoverNote(row: LeftoverDraft): { text: string; warn: boolean } | null {
  if (row.warning !== null) {
    return { text: WARNINGS[row.warning] ?? 'Don’t send this draft.', warn: true }
  }
  if (row.keep_for_check) {
    return { text: 'Keep it until the mailbox’s Message-ID check passes.', warn: false }
  }
  if (row.low_priority) {
    return { text: 'They replied. You may still want to send it as your answer.', warn: false }
  }
  return null
}
