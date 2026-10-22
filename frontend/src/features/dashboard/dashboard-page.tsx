import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { useEffect, useState } from 'react'

import { toneClasses } from '@/components/ui/tone'
import { attentionQuery } from '@/features/attention/api'
import { LinkedInStepsSection } from '@/features/linkedin-steps/linkedin-steps-section'
import { cn } from '@/lib/utils'

import { ChangedJobsCard, CampaignsCard, ComingUpCard, RepliesCard } from './glance-cards'
import { LiveRunCard } from './live-run-card'
import { MetricTiles } from './metric-tiles'
import { SetupChecklist } from './setup-checklist'
import { NEEDS_YOU_ID, needsYouCount, needsYouText, type Problem } from './status'
import { NeedsYouChip, ProblemList } from './status-chips'
import { useDashboardStatus } from './use-dashboard-status'

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
 * you, with a chip that goes to them, the metric tiles (#740), the setup checklist (#747), and
 * the detail: Needs you, Running now, Coming up, Campaigns and Changed jobs.
 *
 * The top bar holds the page's `<h1>`, "Dashboard", so the header here is the line
 * under it. Every region reads its own queries and has its own loading and error
 * states, so one failed read never blanks the page.
 */
export function DashboardPage() {
  const { problems } = useDashboardStatus()
  const attention = useQuery(attentionQuery)
  const now = useMinute()
  const count = needsYouCount(problems, attention.data)

  return (
    <div className="flex max-w-6xl flex-col gap-4">
      <header
        data-region="header"
        className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2"
      >
        <div className="flex flex-col gap-0.5">
          <span className="nk-eyebrow">{eyebrowText(now)}</span>
          <p data-testid="needs-you-line">{needsYouText(count)}</p>
        </div>
        {count !== null && count > 0 && (
          <NeedsYouChip count={count} stopped={problems.length > 0} />
        )}
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
