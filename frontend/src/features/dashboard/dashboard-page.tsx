import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { useEffect, useState } from 'react'

import { toneClasses } from '@/components/ui/tone'
import { attentionQuery } from '@/features/attention/api'
import { useEventStreamStatus } from '@/features/events/event-stream-context'
import { useServerEvent } from '@/features/events/use-server-event'
import { linkedinKeys } from '@/features/linkedin/api'
import { LinkedInStepsSection } from '@/features/linkedin-steps/linkedin-steps-section'
import { cn } from '@/lib/utils'

import { ChangedJobsCard, CampaignsCard, ComingUpCard, RepliesCard } from './glance-cards'
import { LiveRunCard } from './live-run-card'
import { MetricTiles } from './metric-tiles'
import { SetupChecklist } from './setup-checklist'
import { NEEDS_YOU_ID, needsYouCount, needsYouText, type Problem } from './status'
import { ProblemList, StatusChips } from './status-chips'
import { useDashboardStatus } from './use-dashboard-status'

/**
 * A LinkedIn run starting or ending moves the schedule, the last run, the
 * session flag, budget, and heat, and none of them says so on its own; refetch
 * them all once per run, as the LinkedIn page does (`use-run-events.ts`).
 */
function useLinkedInRefresh(): void {
  const queryClient = useQueryClient()
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: linkedinKeys.all })
  }
  useServerEvent('run.started', refresh)
  useServerEvent('run.finished', refresh)
}

/** The current time, moved on each minute, for the header's eyebrow. */
function useMinute(): Date {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 60_000)
    return () => window.clearInterval(timer)
  }, [])
  return now
}

/** "Tuesday, October 20 · 2:05 PM", in the reader's own locale and zone. */
function eyebrowText(now: Date): string {
  const day = now.toLocaleDateString(undefined, {
    weekday: 'long',
    month: 'long',
    day: 'numeric',
  })
  const time = now.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
  return `${day} · ${time}`
}

/**
 * The dashboard, one page (#739), top to bottom: a header saying how many things need
 * you, the status chips, the metric tiles (#740), the setup checklist (#747), and
 * the detail: Needs you, Running now, Coming up, Campaigns and Changed jobs.
 *
 * The top bar holds the page's `<h1>`, "Dashboard", so the header here is the line
 * under it. Every region reads its own queries and has its own loading and error
 * states, so one failed read never blanks the page.
 */
export function DashboardPage() {
  const { chips, problems } = useDashboardStatus()
  const attention = useQuery(attentionQuery)
  const now = useMinute()
  useLinkedInRefresh()

  return (
    <div className="flex max-w-6xl flex-col gap-4">
      <header data-region="header" className="flex flex-col gap-3">
        <div className="flex flex-wrap items-end justify-between gap-2">
          <div className="flex flex-col gap-0.5">
            <span className="nk-eyebrow">{eyebrowText(now)}</span>
            <p data-testid="needs-you-line">
              {needsYouText(needsYouCount(problems, attention.data))}
            </p>
          </div>
          <LiveUpdates />
        </div>
        <StatusChips chips={chips} />
      </header>

      <MetricTiles />

      <SetupChecklist />

      <div className="grid gap-4 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <NeedsYou problems={problems} inbox={attention.data?.counts.inbox ?? 0} />
        <div className="flex flex-col gap-4">
          <section aria-label="Running now" data-region="running">
            <LiveRunCard />
          </section>
          <div data-region="coming-up">
            <ComingUpCard />
          </div>
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <div data-region="campaigns">
          <CampaignsCard />
        </div>
        <div data-region="changed-jobs">
          <ChangedJobsCard />
        </div>
      </div>
    </div>
  )
}

/**
 * Needs you: a tinted pane, most urgent first. What stopped comes first, with its fix,
 * then the unhandled Inbox items, the replies, and the LinkedIn steps waiting for you.
 */
function NeedsYou({ problems, inbox }: { problems: Problem[]; inbox: number }) {
  return (
    <section
      id={NEEDS_YOU_ID}
      aria-labelledby="needs-you-heading"
      data-region="needs-you"
      className={cn(
        'nk-glass flex scroll-mt-4 flex-col gap-3 p-(--space-card-sm)',
        toneClasses('accent'),
      )}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2
          id="needs-you-heading"
          className="font-heading text-card-title font-bold tracking-heading"
        >
          Needs you
        </h2>
        <span className="text-xs text-muted-foreground">Most urgent first</span>
      </div>
      <ProblemList problems={problems} />
      {inbox > 0 && (
        <p className="flex flex-wrap items-center gap-2 text-sm">
          <span>
            <span className="font-medium tabular-nums">{inbox}</span> unhandled in the Inbox.
          </span>
          <Link to="/inbox" className="text-accent-ink underline-offset-2 hover:underline">
            Open the Inbox
          </Link>
        </p>
      )}
      <RepliesCard level={3} />
      <LinkedInStepsSection level={3} />
    </section>
  )
}

/** Whether the page gets live updates, as a small dot and a word. */
function LiveUpdates() {
  const stream = useEventStreamStatus()
  return (
    <span className="flex items-center gap-2 text-sm text-muted-foreground">
      <span
        aria-hidden="true"
        className={cn(
          'size-2 rounded-full',
          stream === 'connected' ? 'bg-ok' : 'bg-muted-foreground/50',
        )}
      />
      <span role="status">
        Live updates {stream === 'connected' ? 'connected' : 'disconnected'}
      </span>
    </span>
  )
}
