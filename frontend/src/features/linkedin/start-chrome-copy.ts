/**
 * What **Start Chrome** says (#599): each launch outcome and refusal in plain words,
 * with what to do. Kept apart from the component so tests and the component share it.
 */
import type { Tone } from '@/components/ui/tone'

import { ApiError } from './api'

/** `browser.launch`'s payload (`netkeep/services/chrome_launch.py`, `submit_launch`). */
export interface LaunchEvent {
  task_id: string
  status: 'started' | 'handed_off' | 'shown' | 'handoff_unconfirmed' | 'refused' | 'failed'
  refusal: string | null
  message: string
  pid: number | null
}

/** How long the button waits to hear how a launch went before it says it didn't. The
 * launcher settles in about 5 s after its checks; this leaves room for a slow machine. */
export const LAUNCH_WAIT_MS = 30_000

export interface Said {
  /** The callout's tint (#606): `ok` for a launch that reached Chrome, `warn` for a
   * refusal or a wait, `bad` for a failure. Anything but `ok` is announced as an alert. */
  tone: Extract<Tone, 'ok' | 'warn' | 'bad'>
  text: string
  detail?: string
}

const REFUSALS: Record<string, string> = {
  not_dedicated:
    "Another Chrome is using netkeep's debug port, so netkeep started nothing. Quit that Chrome, or change the Chrome debug address in Settings, then try again.",
  profile_without_port:
    "A Chrome has netkeep's profile open without the debug port, so runs couldn't use it, and netkeep started nothing. Quit that Chrome, then try again.",
  run_holds_browser:
    'A run is using the browser, so netkeep started nothing. Try again when the run ends.',
  remote_address:
    'The Chrome debug address in Settings points at another computer, and netkeep starts Chrome only on this one. Change it to this computer, then try again.',
  invalid_port:
    'The Chrome debug address in Settings has no valid port, and netkeep never guesses one. Fix it, then try again.',
}

/** A launch's outcome in plain words, with what to do next. */
export function describeLaunch(event: LaunchEvent): Said {
  switch (event.status) {
    case 'started':
      return {
        tone: 'ok',
        text: "netkeep's Chrome started, with netkeep in its own window. Sign in to LinkedIn there yourself if it asks: netkeep never types your password.",
      }
    case 'handed_off':
      return {
        tone: 'ok',
        text: "netkeep's Chrome was already running. It opened netkeep in a new window.",
      }
    case 'shown':
      return {
        tone: 'ok',
        text: "netkeep's Chrome was already running with netkeep open. It brought that window to the front.",
      }
    case 'handoff_unconfirmed':
      return {
        tone: 'ok',
        text: "netkeep's Chrome is running, but netkeep couldn't confirm the new window opened. Look for it in netkeep's Chrome.",
        detail: event.message,
      }
    case 'refused':
      return {
        tone: 'warn',
        text: REFUSALS[event.refusal ?? ''] ?? "netkeep didn't start Chrome. Nothing was started.",
        detail: event.message,
      }
    default:
      return {
        tone: 'bad',
        text: "Chrome didn't start. Check that Google Chrome is installed, then try again.",
        detail: event.message,
      }
  }
}

/** A refused press (the request itself, before any launch), in plain words. */
export function describeRequestError(error: unknown): Said {
  if (error instanceof ApiError && error.status === 409) {
    return { tone: 'warn', text: "netkeep's Chrome is already starting. Wait for it." }
  }
  if (error instanceof ApiError && error.status === 503) {
    return {
      tone: 'bad',
      text: "This netkeep wasn't started with netkeep serve, so it can't start Chrome. Start it from a terminal instead.",
    }
  }
  return {
    tone: 'bad',
    text: "netkeep couldn't ask for Chrome to start.",
    detail: error instanceof Error ? error.message : String(error),
  }
}
