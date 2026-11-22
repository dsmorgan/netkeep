import type { Position } from './api'

/** Each LinkedIn position in a line, under its stop here and in the page bar (#835). */
export const LINKEDIN_LINES: Record<Position, string> = {
  off: 'Leaves LinkedIn alone',
  manual: 'Runs when you click Start',
  automatic: 'Runs on its own, within active hours',
}

/** Each Gmail position in a line; `replyEvery` is the reply check's interval. */
export function gmailLines(replyEvery: string): Record<Position, string> {
  return {
    off: 'Leaves Gmail alone',
    manual: 'Writes drafts; you send them',
    automatic: `Sends within sending hours; checks replies ${replyEvery}`,
  }
}

/** The server's reasons come without a full stop; the page shows them as sentences. */
export function sentence(text: string): string {
  return /[.!?]$/.test(text) ? text : `${text}.`
}
