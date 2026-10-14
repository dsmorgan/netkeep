import type { Attention } from './api'

export type ActivityState = Attention['activity']['state']

/**
 * The indicator's short text, one per state (#347). "Blocked", not "stopped": a
 * flag, heat, a breaker or the auto-send hold blocks a run from starting, whether or
 * not scheduled runs were armed, so the word must not suggest one was going.
 */
export const ACTIVITY_TEXT: Record<ActivityState, string> = {
  running: 'LinkedIn: running',
  stopped: 'LinkedIn: blocked',
  paused: 'LinkedIn: paused',
  armed: 'LinkedIn: armed',
  idle: 'LinkedIn: off',
}

export const FAVICON = '/favicon.svg'
export const FAVICON_RUNNING = '/favicon-running.svg'

/** The tab icon: the running one only while a LinkedIn run is in progress. */
export function faviconFor(attention: Attention | undefined): string {
  return attention?.activity.state === 'running' ? FAVICON_RUNNING : FAVICON
}

/** What the live region says when the state changes: the short text and its sentence. */
export function announcement(activity: Attention['activity']): string {
  const text = ACTIVITY_TEXT[activity.state]
  return activity.label === '' ? text : `${text}. ${activity.label}`
}
