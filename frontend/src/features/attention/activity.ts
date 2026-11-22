import { POSITION_LABEL, type Position } from '@/features/automation/api'

import type { Attention } from './api'

export type ActivityState = Attention['activity']['state']

/**
 * The live region's short text, one per state (#347). "Blocked", not "stopped": a
 * flag, heat, a breaker or the auto-send hold blocks a run from starting, whether or
 * not scheduled runs were on, so the word must not suggest one was going.
 *
 * Armed is the switch on Automatic, so it says so (#835). Idle is Manual or Off: the
 * announcement names whichever the switch reads, and this neutral text stands in only
 * while the switch hasn't answered, so it never says "off" over a switch on Manual.
 */
export const ACTIVITY_TEXT: Record<ActivityState, string> = {
  running: 'LinkedIn: running',
  stopped: 'LinkedIn: blocked',
  armed: 'LinkedIn: Automatic',
  idle: 'LinkedIn: not on Automatic',
}

export const FAVICON = '/favicon.svg'
export const FAVICON_RUNNING = '/favicon-running.svg'

/**
 * The tab icon: the running one while a LinkedIn run is in progress or a campaign send
 * batch just ran (#547).
 */
export function faviconFor(attention: Attention | undefined, campaignSending = false): string {
  return attention?.activity.state === 'running' || campaignSending ? FAVICON_RUNNING : FAVICON
}

/**
 * What the live region says when the state changes: the short text and its sentence.
 * `position` is the LinkedIn switch's, when known: idle then names it (Manual or Off).
 */
export function announcement(activity: Attention['activity'], position?: Position): string {
  const text =
    activity.state === 'idle' && position !== undefined && position !== 'automatic'
      ? `LinkedIn: ${POSITION_LABEL[position]}`
      : ACTIVITY_TEXT[activity.state]
  return activity.label === '' ? text : `${text}. ${activity.label}`
}
