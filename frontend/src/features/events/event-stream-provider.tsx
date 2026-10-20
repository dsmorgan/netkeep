import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, type ReactNode } from 'react'

import { EventStreamContext } from './event-stream-context'
import { useEventStream } from './use-event-stream'

const EVENTS_URL = '/api/v1/events'

/** Mounts once in the root layout so the whole UI shares one SSE connection. */
export function EventStreamProvider({ children }: { children: ReactNode }) {
  const stream = useEventStream(EVENTS_URL)
  const queryClient = useQueryClient()
  const everConnected = useRef(false)
  const previousStatus = useRef(stream.status)

  useEffect(() => {
    // SSE is fire-and-forget: nothing replays an event missed while the connection
    // was down, on any page. Refetch every active query the moment it comes *back*,
    // so a dropped run or poll event is a plain refetch, not a stale screen. The
    // first connect misses nothing, so it does not double the initial fetches (#704).
    if (
      stream.status === 'connected' &&
      previousStatus.current === 'disconnected' &&
      everConnected.current
    ) {
      // Skip a query that opted out of focus refetches on purpose (a POST preview, say).
      void queryClient.invalidateQueries({
        predicate: (query) =>
          !query.observers.some((observer) => observer.options.refetchOnWindowFocus === false),
      })
    }
    if (stream.status === 'connected') everConnected.current = true
    previousStatus.current = stream.status
  }, [stream.status, queryClient])

  return <EventStreamContext value={stream}>{children}</EventStreamContext>
}
