/**
 * The status roll-up the top bar's chips read (`GET /system-status`, #761): the
 * server's parts of the Gmail, LinkedIn and netkeep chips, in one read-only call.
 *
 * Most of what it rolls up moves with no event of its own, so it refetches as often as
 * `/attention` does. It also refetches at once on the server events that move a part,
 * after every successful change this tab makes, and whenever the poll status answers
 * (a "Check now" makes that refetch every 15 seconds while it waits).
 */
import { queryOptions, useQuery, useQueryClient } from '@tanstack/react-query'
import type { Middleware } from 'openapi-fetch'
import { useCallback, useEffect } from 'react'

import { api } from '@/api/client'
import { ATTENTION_EVERY_MS, changesAttention } from '@/features/attention/api'
import { CAMPAIGN_TICK_EVENT } from '@/features/attention/use-campaign-sending'
import { useServerEvent } from '@/features/events/use-server-event'
import { pollStatusQuery } from '@/features/poll-status/api'

import type { ServerStatus } from './roll-up'

export const systemStatusKeys = {
  all: ['system-status'] as const,
}

/**
 * How soon to read again while a campaign send batch shows: the server shows it for 15
 * seconds after the tick that sent, and this clears it about when that ends.
 */
export const BATCH_EVERY_MS = 15_000

function sending(status: ServerStatus | undefined): boolean {
  return status?.gmail.parts.some((part) => part.key === 'batch') ?? false
}

export const systemStatusQuery = queryOptions({
  queryKey: systemStatusKeys.all,
  queryFn: async ({ signal }): Promise<ServerStatus> => {
    const { data, response } = await api.GET('/api/v1/system-status', { signal })
    if (data === undefined) {
      throw new Error(`GET /api/v1/system-status returned ${response.status}`)
    }
    return data
  },
  refetchInterval: (query) => (sending(query.state.data) ? BATCH_EVERY_MS : ATTENTION_EVERY_MS),
})

/** The server events that can move a part of a chip. */
export const STATUS_EVENTS = [
  'run.started',
  'run.finished',
  'run.progress',
  'mailbox.status',
  'task.finished',
  CAMPAIGN_TICK_EVENT,
] as const

/**
 * Reads the roll-up, and refetches it on the server events that move it, after every
 * successful change this tab makes, and whenever the poll status answers.
 */
export function useSystemStatus() {
  const queryClient = useQueryClient()
  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: systemStatusKeys.all })
  }, [queryClient])
  useServerEvent(STATUS_EVENTS[0], refresh)
  useServerEvent(STATUS_EVENTS[1], refresh)
  useServerEvent(STATUS_EVENTS[2], refresh)
  useServerEvent(STATUS_EVENTS[3], refresh)
  useServerEvent(STATUS_EVENTS[4], refresh)
  useServerEvent(STATUS_EVENTS[5], refresh)

  useEffect(() => {
    const middleware: Middleware = {
      onResponse({ request, response }) {
        if (response.ok && changesAttention(request.method, request.url)) refresh()
        return undefined
      },
    }
    api.use(middleware)
    return () => api.eject(middleware)
  }, [refresh])

  const polled = useQuery(pollStatusQuery).dataUpdatedAt
  useEffect(() => {
    if (polled !== 0) refresh()
  }, [polled, refresh])

  return useQuery(systemStatusQuery)
}
