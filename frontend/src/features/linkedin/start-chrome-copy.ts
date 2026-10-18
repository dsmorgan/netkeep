/**
 * What **Start Chrome** says (#599): each launch outcome and refusal in plain words,
 * with what to do. Kept apart from the component so tests and the component share it.
 */
import { ApiError } from './api'

/** `browser.launch`'s payload (`netkeeper/services/chrome_launch.py`, `submit_launch`). */
export interface LaunchEvent {
  task_id: string
  status: 'started' | 'handed_off' | 'handoff_unconfirmed' | 'refused' | 'failed'
  refusal: string | null
  message: string
  pid: number | null
}

/** How long the button waits to hear how a launch went before it says it didn't. The
 * launcher settles in about 5 s after its checks; this leaves room for a slow machine. */
export const LAUNCH_WAIT_MS = 30_000

export interface Said {
  tone: 'ok' | 'problem'
  text: string
  detail?: string
}

const REFUSALS: Record<string, string> = {
  not_dedicated:
    "Another Chrome is using netkeeper's debug port, so netkeeper started nothing. Quit that Chrome, or change the Chrome debug address in Settings, then try again.",
  profile_without_port:
    "A Chrome has netkeeper's profile open without the debug port, so runs couldn't use it, and netkeeper started nothing. Quit that Chrome, then try again.",
  run_holds_browser:
    'A run is using the browser, so netkeeper started nothing. Try again when the run ends.',
  remote_address:
    'The Chrome debug address in Settings points at another computer, and netkeeper starts Chrome only on this one. Change it to this computer, then try again.',
  invalid_port:
    'The Chrome debug address in Settings has no valid port, and netkeeper never guesses one. Fix it, then try again.',
}

/** A launch's outcome in plain words, with what to do next. */
export function describeLaunch(event: LaunchEvent): Said {
  switch (event.status) {
    case 'started':
      return {
        tone: 'ok',
        text: "netkeeper's Chrome started, with netkeeper in its own window. Sign in to LinkedIn there yourself if it asks: netkeeper never types your password.",
      }
    case 'handed_off':
      return {
        tone: 'ok',
        text: "netkeeper's Chrome was already running. It opened netkeeper in a new window.",
      }
    case 'handoff_unconfirmed':
      return {
        tone: 'ok',
        text: "netkeeper's Chrome is running, but netkeeper couldn't confirm the new window opened. Look for it in netkeeper's Chrome.",
        detail: event.message,
      }
    case 'refused':
      return {
        tone: 'problem',
        text:
          REFUSALS[event.refusal ?? ''] ?? "netkeeper didn't start Chrome. Nothing was started.",
        detail: event.message,
      }
    default:
      return {
        tone: 'problem',
        text: "Chrome didn't start. Check that Google Chrome is installed, then try again.",
        detail: event.message,
      }
  }
}

/** A refused press (the request itself, before any launch), in plain words. */
export function describeRequestError(error: unknown): Said {
  if (error instanceof ApiError && error.status === 409) {
    return { tone: 'problem', text: "netkeeper's Chrome is already starting. Wait for it." }
  }
  if (error instanceof ApiError && error.status === 503) {
    return {
      tone: 'problem',
      text: "This netkeeper wasn't started with netkeeper serve, so it can't start Chrome. Start it from a terminal instead.",
    }
  }
  return {
    tone: 'problem',
    text: "netkeeper couldn't ask for Chrome to start.",
    detail: error instanceof Error ? error.message : String(error),
  }
}
