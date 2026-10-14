/** Labels and rules for recording a response from outside netkeeper (#512). */
import type { EnrollmentStatus, ResponseOutcome, ResponseVia } from './api'

export const VIA_LABELS: Record<ResponseVia, string> = {
  call: 'Phone call',
  text: 'Text message',
  other_email: 'Email from another address',
  other_app: 'Another app',
  in_person: 'In person',
  other: 'Another way',
}

export const OUTCOME_LABELS: Record<ResponseOutcome, string> = {
  positive: 'Positive',
  neutral: 'Neutral',
  not_interested: 'Not interested',
  opt_out: 'Asked to stop',
}

/** Statuses a response may be recorded on: the three that could still send, and completed. */
const RECORDABLE: ReadonlySet<EnrollmentStatus> = new Set([
  'pending',
  'active',
  'paused',
  'completed',
])

/** Statuses that could still send: a response ends these. */
export const STOPPABLE: ReadonlySet<EnrollmentStatus> = new Set(['pending', 'active', 'paused'])

export interface ResponseTarget {
  id: number
  campaignName: string
  status: EnrollmentStatus
}

export function canRecordOn(status: EnrollmentStatus): boolean {
  return RECORDABLE.has(status)
}
