import { useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query'
import { Component, useCallback, useEffect, useRef, useState, type ReactNode } from 'react'

import { healthQuery } from '@/api/queries'
import { announcement, type ActivityState } from '@/features/attention/activity'
import { attentionQuery } from '@/features/attention/api'
import { lastRunQuery } from '@/features/dashboard/api'
import { useEventStreamStatus } from '@/features/events/event-stream-context'
import { useServerEvent } from '@/features/events/use-server-event'
import { budgetQuery, heatQuery, scheduleQuery, statusQuery } from '@/features/linkedin/api'
import { pollStatusQuery, usePollStatusRefresh } from '@/features/poll-status/api'
import { CheckRepliesNow } from '@/features/poll-status/check-now'
import { useNow } from '@/features/poll-status/use-now'

import { useSystemStatus } from './api'
import { gmailStatus, linkedinStatus, netkeepStatus, read, updatedText } from './roll-up'
import { StatusChip } from './status-chip'

/** When the newest of `queries` last answered, in ms; 0 before any has. */
function updatedAt(...queries: UseQueryResult<unknown>[]): number {
  return Math.max(0, ...queries.map((query) => query.dataUpdatedAt))
}

/**
 * Keeps the LinkedIn reads other pages share fresh (the dashboard's problems read the
 * session flag and heat). A LinkedIn run starting or ending moves the session flag, the
 * schedule, the last run, budget and heat, and none of them says so on its own, so the
 * five refetch once per run, and nothing else under `linkedinKeys` (the LinkedIn page
 * invalidates its own). The poll status refetches on its own events. The chips read
 * the roll-up, which refetches on its own events (`useSystemStatus`).
 */
function useStatusRefresh(): void {
  const queryClient = useQueryClient()
  const linkedin = useCallback(() => {
    for (const { queryKey } of [statusQuery, scheduleQuery, budgetQuery, heatQuery, lastRunQuery]) {
      void queryClient.invalidateQueries({ queryKey, exact: true })
    }
  }, [queryClient])
  useServerEvent('run.started', linkedin)
  useServerEvent('run.finished', linkedin)
  usePollStatusRefresh()
}

/**
 * The polite live region for LinkedIn (#347): it speaks only when the activity state
 * changes, never on the first answer or on a refetch that changes nothing.
 */
function LinkedInAnnouncer() {
  const attention = useQuery(attentionQuery)
  const activity = attention.data?.activity
  const [spoken, setSpoken] = useState('')
  const previous = useRef<ActivityState | null>(null)

  useEffect(() => {
    if (activity === undefined) return
    if (previous.current !== null && previous.current !== activity.state) {
      setSpoken(announcement(activity))
    }
    previous.current = activity.state
  }, [activity])

  return (
    <span role="status" aria-live="polite" className="sr-only" data-testid="activity-live">
      {spoken}
    </span>
  )
}

/** How long a status bar that failed to render waits before it tries again. */
const RETRY_AFTER_MS = 15_000

/**
 * Keeps an answer the chips can't read (a proxy page, an older backend) from taking
 * down every page: the bar says so, and tries again shortly.
 */
class StatusBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false }
  private timer: ReturnType<typeof setTimeout> | null = null

  static getDerivedStateFromError() {
    return { failed: true }
  }

  componentDidCatch() {
    this.timer = setTimeout(() => this.setState({ failed: false }), RETRY_AFTER_MS)
  }

  componentWillUnmount() {
    if (this.timer !== null) clearTimeout(this.timer)
  }

  render() {
    if (!this.state.failed) return this.props.children
    return (
      <span role="status" className="text-xs text-muted-foreground">
        Status couldn’t be shown; trying again shortly
      </span>
    )
  }
}

/**
 * The top bar's three status chips (#751), Gmail, LinkedIn and netkeep, on every page.
 * They read the server's roll-up (`GET /system-status`, #761) and add what only the UI
 * knows; nothing here starts anything but "Check now" in the Gmail popover.
 */
export function SystemStatusChips() {
  return (
    <StatusBoundary>
      <Chips />
    </StatusBoundary>
  )
}

function Chips() {
  const health = useQuery(healthQuery)
  // For "Check now" in the Gmail popover; the chips read the roll-up.
  const poll = useQuery(pollStatusQuery)
  const status = useSystemStatus()
  const stream = useEventStreamStatus()
  const now = useNow()
  useStatusRefresh()

  const serverDown = health.isError
  const reads = { status: read(status), serverDown }
  const gmail = gmailStatus(reads, now)
  const linkedin = linkedinStatus(reads, now)
  const app = netkeepStatus({ health: read(health), stream, status: read(status) }, now)
  const replies = poll.data?.items.find((check) => check.key === 'gmail_replies')
  const updated = updatedText(updatedAt(status), now)

  return (
    <div
      role="group"
      aria-label="System status"
      className="flex min-w-0 flex-wrap items-center justify-end gap-1.5"
    >
      <StatusChip status={gmail} updated={updated}>
        {replies !== undefined && !serverDown && (
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
            <span>Ask for the reply check at the next tick.</span>
            <CheckRepliesNow check={replies} />
          </div>
        )}
      </StatusChip>
      <StatusChip status={linkedin} updated={updated} />
      <LinkedInAnnouncer />
      <StatusChip status={app} updated={updatedText(updatedAt(health, status), now)} />
    </div>
  )
}
