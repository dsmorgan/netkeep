/**
 * The status roll-up the top bar's chips read (`GET /system-status`, #761): the
 * server's parts of the Gmail, LinkedIn and netkeep chips, in one read-only call.
 *
 * Most of what it rolls up moves with no event of its own, so it refetches as often as
 * `/attention` does. It also refetches on the server events that move a part (a run's
 * progress at most every 5 seconds),
 * after every successful change this tab makes, and whenever the poll status answers
 * (a "Check now" makes that refetch every 15 seconds while it waits).
 */
import { queryOptions, useQuery, useQueryClient } from '@tanstack/react-query'
import type { Middleware } from 'openapi-fetch'
import { useCallback, useEffect, useRef } from 'react'

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

/** The server events that can move a part of a chip; `run.progress` is throttled. */
export const STATUS_EVENTS = [
  'run.started',
  'run.finished',
  'mailbox.status',
  'task.finished',
  CAMPAIGN_TICK_EVENT,
] as const

/**
 * At most one refetch per this long for `run.progress`, which a run sends per visit or
 * page: the first event reads at once, and the last one in each window reads at its end.
 */
export const PROGRESS_EVERY_MS = 5_000

/** `refresh` at most once per `every` ms: at once, then once more at the window's end. */
function useThrottled(refresh: () => void, every: number): () => void {
  const last = useRef(0)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(
    () => () => {
      if (timer.current !== null) clearTimeout(timer.current)
    },
    [],
  )
  return useCallback(() => {
    if (timer.current !== null) return // a trailing read is already due
    const wait = last.current + every - Date.now()
    const fire = () => {
      timer.current = null
      last.current = Date.now()
      refresh()
    }
    if (wait <= 0) fire()
    else timer.current = setTimeout(fire, wait)
  }, [refresh, every])
}

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
  useServerEvent('run.progress', useThrottled(refresh, PROGRESS_EVERY_MS))

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
