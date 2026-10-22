import { useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query'
import { Component, useCallback, useEffect, useRef, useState, type ReactNode } from 'react'

import { healthQuery } from '@/api/queries'
import { announcement, type ActivityState } from '@/features/attention/activity'
import { attentionQuery } from '@/features/attention/api'
import { useCampaignSending } from '@/features/attention/use-campaign-sending'
import { lastRunQuery } from '@/features/dashboard/api'
import { useEventStreamStatus } from '@/features/events/event-stream-context'
import { useServerEvent } from '@/features/events/use-server-event'
import {
  budgetQuery,
  heatQuery,
  linkedinKeys,
  scheduleQuery,
  statusQuery,
} from '@/features/linkedin/api'
import { mailboxKeys, mailboxListQuery } from '@/features/mailboxes/api'
import { pollStatusQuery, usePollStatusRefresh } from '@/features/poll-status/api'
import { CheckRepliesNow } from '@/features/poll-status/check-now'
import { useNow } from '@/features/poll-status/use-now'
import { useKeyPersisted } from '@/features/session/key-persisted'

import {
  gmailStatus,
  linkedinStatus,
  netkeepStatus,
  read,
  updatedText,
  type RunProgress,
} from './roll-up'
import { StatusChip } from './status-chip'

/** When the newest of `queries` last answered, in ms; 0 before any has. */
function updatedAt(...queries: UseQueryResult<unknown>[]): number {
  return Math.max(0, ...queries.map((query) => query.dataUpdatedAt))
}

/**
 * Keeps the chips' reads fresh. The mailbox list refreshes on the `mailbox.status`
 * event the backend's poll publishes, so a status change shows without a reload; it is
 * the list, never `/mailboxes/status`, so it still answers while the Keychain is locked.
 * A LinkedIn run starting or ending moves the session flag, the schedule, the last run,
 * budget and heat, and none of them says so on its own, so each refetches once per run.
 * The poll status refetches on its own events. The top bar is on every page, so these
 * are the app's.
 */
function useStatusRefresh(): void {
  const queryClient = useQueryClient()
  const mailboxes = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: mailboxKeys.all })
  }, [queryClient])
  const linkedin = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: linkedinKeys.all })
  }, [queryClient])
  useServerEvent('mailbox.status', mailboxes)
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
 * Each reads the queries its own page uses, so the cache is one and they cannot
 * disagree; nothing here starts anything but "Check now" in the Gmail popover.
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
  const poll = useQuery(pollStatusQuery)
  const mailboxes = useQuery(mailboxListQuery)
  const status = useQuery(statusQuery)
  const lastRun = useQuery(lastRunQuery)
  const schedule = useQuery(scheduleQuery)
  const budget = useQuery(budgetQuery)
  const heat = useQuery(heatQuery)
  const attention = useQuery(attentionQuery)
  const stream = useEventStreamStatus()
  const keyPersisted = useKeyPersisted()
  const sending = useCampaignSending()
  const now = useNow()
  const [progress, setProgress] = useState<RunProgress | null>(null)
  useServerEvent<RunProgress>('run.progress', setProgress)
  useStatusRefresh()

  const serverDown = health.isError
  const gmail = gmailStatus(
    { mailboxes: read(mailboxes), poll: read(poll), sending, serverDown },
    now,
  )
  const linkedin = linkedinStatus(
    {
      status: read(status),
      lastRun: lastRun.data?.items[0],
      progress,
      schedule: read(schedule),
      budget: read(budget),
      heat: read(heat),
      poll: read(poll),
      attention: attention.data,
      serverDown,
    },
    now,
  )
  const app = netkeepStatus({ health: read(health), stream, poll: read(poll), keyPersisted })
  const replies = poll.data?.items.find((check) => check.key === 'gmail_replies')

  return (
    <div
      role="group"
      aria-label="System status"
      className="flex min-w-0 flex-wrap items-center justify-end gap-1.5"
    >
      <StatusChip status={gmail} updated={updatedText(updatedAt(mailboxes, poll), now)}>
        {replies !== undefined && !serverDown && (
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
            <span>Ask for the reply check at the next tick.</span>
            <CheckRepliesNow check={replies} />
          </div>
        )}
      </StatusChip>
      <StatusChip
        status={linkedin}
        updated={updatedText(
          updatedAt(status, lastRun, schedule, budget, heat, poll, attention),
          now,
        )}
      />
      <LinkedInAnnouncer />
      <StatusChip status={app} updated={updatedText(updatedAt(health, poll), now)} />
    </div>
  )
}
