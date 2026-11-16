/**
 * One automation switch per service, Off, Manual or Automatic (#829), through the
 * generated client: `GET /automation`, `PUT /automation/linkedin` and
 * `PUT /automation/gmail`. Each `PUT` answers the same shape as the `GET`.
 *
 * A move that does more (to Automatic, or LinkedIn's depth from quick to full) answers
 * 422 without `confirm: true`; the UI asks first and sends it. Gmail Automatic answers
 * 409 while a mailbox lacks its Message-ID check (#510), with `gmail.blocked_reason`'s
 * sentence as the detail.
 */
import { queryOptions } from '@tanstack/react-query'

import { api } from '@/api/client'
import { detailMessage } from '@/api/errors'
import type { components } from '@/api/schema'
import { ATTENTION_EVERY_MS } from '@/features/attention/api'

export type Automation = components['schemas']['AutomationOut']
export type Position = components['schemas']['Position']
export type Depth = components['schemas']['ScheduleDepth']
export type Service = keyof Automation

/** Left to right: a stop further right does more. */
export const POSITIONS: readonly Position[] = ['off', 'manual', 'automatic']

/**
 * The job kinds the schedule fires only with the weekly full sync (the scheduler's
 * `FULL_UPKEEP_KINDS`): at quick checks it skips them, so their due times never fire.
 */
export const FULL_UPKEEP_KINDS: ReadonlySet<string> = new Set(['connections_full', 'enrich'])

export const POSITION_LABEL: Record<Position, string> = {
  off: 'Off',
  manual: 'Manual',
  automatic: 'Automatic',
}

export const DEPTH_LABEL: Record<Depth, string> = {
  quick: 'Quick checks',
  full: 'Quick checks and a weekly full sync',
}

export const automationKeys = {
  all: ['automation'] as const,
}

/** A refused move: the server's sentence, and its status (422 or 409). */
export class AutomationError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'AutomationError'
    this.status = status
  }
}

function failure(error: unknown, response: Response, what: string): AutomationError {
  return new AutomationError(
    detailMessage(error) ?? `${what} returned ${response.status}`,
    response.status,
  )
}

export const automationQuery = queryOptions({
  queryKey: automationKeys.all,
  queryFn: async ({ signal }): Promise<Automation> => {
    const { data, error, response } = await api.GET('/api/v1/automation', { signal })
    if (data === undefined) throw failure(error, response, 'GET /automation')
    return data
  },
  // The CLI and other tabs move it too, with no event: as often as `/attention` reads.
  refetchInterval: ATTENTION_EVERY_MS,
})

export interface LinkedInMove {
  position: Position
  /** Omitted: the server keeps the stored depth. */
  depth?: Depth
  confirm: boolean
}

export interface GmailMove {
  position: Position
  confirm: boolean
}

export async function setLinkedInAutomation(body: LinkedInMove): Promise<Automation> {
  const { data, error, response } = await api.PUT('/api/v1/automation/linkedin', { body })
  if (data === undefined) throw failure(error, response, 'PUT /automation/linkedin')
  return data
}

export async function setGmailAutomation(body: GmailMove): Promise<Automation> {
  const { data, error, response } = await api.PUT('/api/v1/automation/gmail', { body })
  if (data === undefined) throw failure(error, response, 'PUT /automation/gmail')
  return data
}

/**
 * Whether a move does more, so it asks first: to Automatic from anywhere else, or
 * LinkedIn's depth from quick to full. The server refuses the same moves without
 * `confirm: true`; this only decides when to show the dialog.
 */
export function doesMore(
  from: { position: Position; depth?: Depth },
  to: { position: Position; depth?: Depth },
): boolean {
  if (to.position === 'automatic' && from.position !== 'automatic') return true
  return from.depth === 'quick' && to.depth === 'full'
}
