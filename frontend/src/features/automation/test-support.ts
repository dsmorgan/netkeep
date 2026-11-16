/**
 * Fixtures for the automation switch's tests (#829). Only test files import this;
 * nothing here is real data.
 */
import type { Automation } from './api'

/** What a new install answers: LinkedIn Manual at full depth, Gmail Off with no mailbox. */
export const AUTOMATION_MANUAL: Automation = {
  linkedin: { position: 'manual', depth: 'full', changed_at: null },
  gmail: {
    position: 'manual',
    automatic_ready: true,
    blocked_reason: null,
    changed_at: null,
  },
}

export function automation({
  linkedin = {},
  gmail = {},
}: {
  linkedin?: Partial<Automation['linkedin']>
  gmail?: Partial<Automation['gmail']>
} = {}): Automation {
  return {
    linkedin: { ...AUTOMATION_MANUAL.linkedin, ...linkedin },
    gmail: { ...AUTOMATION_MANUAL.gmail, ...gmail },
  }
}

/** The 409 detail and `blocked_reason` while a mailbox lacks its Message-ID check (#510). */
export const BLOCKED_REASON =
  "netkeep hasn't found one of its own drafts on someone@example.test by its Message-ID yet" +
  ' (waiting: no draft yet; send a test from a campaign’s review)'
