import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import type { ReactNode } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import type { Tone } from '@/components/ui/tone'
import { CampaignStatusBadge } from '@/features/campaigns/badges'
import { campaignsQuery } from '@/features/campaigns/api'
import { countsText } from '@/features/campaigns/format'
import { scheduleQuery } from '@/features/linkedin/api'
import {
  autoSendCheckWhen,
  autoSendStepText,
  autoSendWaitText,
  type AutoSendOutlook,
} from '@/features/linkedin-steps/format'
import { SchedulePause } from '@/features/linkedin/schedule-pause'
import { formatWhen } from '@/features/linkedin/fields'
import { RUN_KIND_LABELS, type RunKind } from '@/features/linkedin/types'

import { changedJobsQuery, inboundQuery, nextFiresQuery, type NextFire } from './api'

/**
 * The dashboard's detail cards (P3-12, regrouped in #739): what happens next, the
 * campaigns, replies and changed jobs. Each card reads the one endpoint that already
 * owns its answer (the schedule the same `/linkedin/schedule` query the LinkedIn page
 * uses, so the two pages share one cache and cannot disagree), and each has its own
 * loading, error, and empty state, so one failing read never hides the rest. Whether
 * anything is unhealthy is the status chips' job (`status-chips.tsx`).
 */

function GlanceCard({
  title,
  description,
  tone,
  level = 2,
  children,
}: {
  title: string
  description?: ReactNode
  /** Tints the card when it holds something that needs you (#606). */
  tone?: Tone
  /** 3 inside a dashboard region with a heading of its own, such as Needs you. */
  level?: 2 | 3
  children: ReactNode
}) {
  return (
    <Card size="sm" tone={tone}>
      <CardHeader>
        <CardTitle level={level}>{title}</CardTitle>
        {description !== undefined && <CardDescription>{description}</CardDescription>}
      </CardHeader>
      <CardContent className="flex flex-col gap-2 text-sm">{children}</CardContent>
    </Card>
  )
}

function Checking() {
  return (
    <p role="status" className="text-muted-foreground">
      Checking…
    </p>
  )
}

function Failed({ what }: { what: string }) {
  return (
    <p role="alert" className="text-muted-foreground">
      {what} could not be loaded.
    </p>
  )
}

function Muted({ children }: { children: ReactNode }) {
  return <p className="text-muted-foreground">{children}</p>
}

function More({ shown, total }: { shown: number; total: number }) {
  if (total <= shown) return null
  return <Muted>and {total - shown} more</Muted>
}

function kindLabel(kind: string): string {
  return RUN_KIND_LABELS[kind as RunKind] ?? kind
}

/**
 * "due now" for a step whose time had come when the list was fetched (the
 * next tick's), else the time. Measured against the fetch, not the clock, so a
 * render stays pure; the list refetches every minute anyway.
 */
function dueText(due: string, fetchedAt: number): string {
  return new Date(due).getTime() <= fetchedAt ? 'due now' : formatWhen(due)
}

/** The day of an instant, in the reader's own timezone, as the reader writes dates. */
function formatDay(iso: string): string {
  const parsed = new Date(iso)
  if (Number.isNaN(parsed.getTime())) return iso
  return parsed.toLocaleDateString(undefined, { dateStyle: 'medium' })
}

function stepText(fire: NextFire, autoSendOn: boolean): string {
  const step = fire.step_position === null ? 'no step left' : `step ${fire.step_position}`
  // An auto-send step sends on its own while auto-send is on; the line under it says when.
  if (autoSendOn && fire.auto_send === true) {
    return `${fire.campaign_name} · ${step} · LinkedIn, sends on its own`
  }
  // Any other LinkedIn step never sends on its own: once due, it waits for you to prefill it.
  const prefill = fire.ready_to_prefill ? ' · LinkedIn, ready to prefill' : ''
  return `${fire.campaign_name} · ${step}${prefill}`
}

/**
 * The LinkedIn schedule, refetched each minute like the other dashboard reads, so the
 * auto-send check's time and reason never go stale on an open page.
 */
const dashboardScheduleQuery = { ...scheduleQuery, refetchInterval: 60_000 }

// --- what happens next ------------------------------------------------------------------

/** The auto-send check's row in the schedule: its kind, which no other job has. */
const AUTO_SEND_CHECK = 'auto_send'

/** One row of Coming up: a campaign send, a scheduled LinkedIn job, or the auto-send check. */
type Upcoming =
  | { kind: 'send'; at: string; fire: NextFire }
  | { kind: 'job'; at: string; job: string }
  | { kind: 'check'; at: string; check: AutoSendOutlook }

/**
 * What happens next, soonest first, as one timeline (#739): the campaign steps the
 * minute tick will consider (`next_action_at`, spec 11.4) and the scheduled LinkedIn
 * runs, from the scheduler's persisted due times (`GET /linkedin/schedule`), with the
 * auto-send check among them while auto-send is on.
 *
 * Unarmed, no LinkedIn run fires however soon a due time is, so the card says that
 * instead of listing times that will not happen. Paused (#324), it says that instead,
 * with the control to unpause; armed and running, it offers the pause. Each source has
 * its own failure line, so a broken schedule never hides the sends.
 */
export function ComingUpCard() {
  const fires = useQuery(nextFiresQuery)
  const schedule = useQuery(dashboardScheduleQuery)
  const outlook = schedule.data?.auto_send ?? null
  const autoSendOn = outlook?.on === true
  const fetchedAt = new Date(fires.dataUpdatedAt)

  if (fires.isPending || schedule.isPending) {
    return (
      <GlanceCard title="Coming up">
        <Checking />
      </GlanceCard>
    )
  }

  const plan = schedule.data
  // Jobs fire only while the schedule is armed and not paused.
  const firing = plan !== undefined && plan.armed && !plan.paused
  const check = firing && autoSendOn ? outlook : null
  const rows: Upcoming[] = [
    ...(fires.data?.items ?? []).map((fire): Upcoming => ({ kind: 'send', at: fire.due, fire })),
    ...(firing ? plan.jobs : []).flatMap((job): Upcoming[] =>
      job.next_due === null ? [] : [{ kind: 'job', at: job.next_due, job: job.kind }],
    ),
  ]
  // An overdue check runs once in the scheduler's catch-up window, not at its old time.
  const checkAt = check === null ? null : check.catch_up ? check.checked_at : check.next_fire
  if (check !== null && checkAt !== null) rows.push({ kind: 'check', at: checkAt, check })
  rows.sort((a, b) => new Date(a.at).getTime() - new Date(b.at).getTime())

  return (
    <GlanceCard title="Coming up">
      {fires.isError && <Failed what="Upcoming sends" />}
      {schedule.isError && <Failed what="The LinkedIn schedule" />}
      {plan !== undefined && !plan.armed && (
        <>
          <Muted>
            Scheduled runs are off. Nothing visits LinkedIn on its own until you arm them.
          </Muted>
          <div>
            <Button size="sm" variant="outline" render={<Link to="/linkedin" hash="schedule" />}>
              Open LinkedIn
            </Button>
          </div>
        </>
      )}
      {firing && !plan.scheduler_running && (
        <p role="alert" className="text-destructive">
          No scheduler is running in this process, so no LinkedIn run below will fire.
        </p>
      )}
      {rows.length === 0 ? (
        <Muted>Nothing is scheduled.</Muted>
      ) : (
        <ol className="flex flex-col gap-1.5">
          {rows.map((row) => (
            <UpcomingRow
              key={rowKey(row)}
              row={row}
              outlook={outlook}
              autoSendOn={autoSendOn}
              fetchedAt={fetchedAt}
            />
          ))}
        </ol>
      )}
      {fires.isSuccess && <More shown={fires.data.items.length} total={fires.data.total} />}
      {check !== null && check.next_fire === null && (
        <p>
          Auto-send check: <span className="text-muted-foreground">{autoSendCheckText(check)}</span>
        </p>
      )}
      {/* Disarmed and paused are separate switches: both say so, and either can be undone. */}
      {plan !== undefined && (plan.armed || plan.paused) && <SchedulePause schedule={plan} />}
    </GlanceCard>
  )
}

function rowKey(row: Upcoming): string {
  if (row.kind === 'send') return `send-${row.fire.enrollment_id}`
  return row.kind === 'job' ? `job-${row.job}` : AUTO_SEND_CHECK
}

function UpcomingRow({
  row,
  outlook,
  autoSendOn,
  fetchedAt,
}: {
  row: Upcoming
  outlook: AutoSendOutlook | null
  autoSendOn: boolean
  fetchedAt: Date
}) {
  if (row.kind === 'job') {
    return (
      <li>
        <span className="font-medium tabular-nums">{formatWhen(row.at)}</span>
        {' — '}
        {kindLabel(row.job)}
      </li>
    )
  }
  if (row.kind === 'check') {
    return (
      <li className="flex flex-col">
        <span>
          <span className="font-medium tabular-nums">{formatWhen(row.at)}</span>
          {' — '}
          Auto-send check
        </span>
        <span className="text-xs text-muted-foreground">{autoSendCheckText(row.check)}</span>
      </li>
    )
  }
  const fire = row.fire
  return (
    <li className="flex flex-col">
      <span>
        <span className="font-medium tabular-nums">{dueText(fire.due, fetchedAt.getTime())}</span>
        {' — '}
        <Link
          to="/contacts/$contactId"
          params={{ contactId: String(fire.contact_id) }}
          className="underline-offset-2 hover:underline"
        >
          {fire.contact_name || 'Unnamed contact'}
        </Link>
      </span>
      <span className="text-xs text-muted-foreground">{stepText(fire, autoSendOn)}</span>
      {autoSendOn && fire.auto_send === true && (
        <span className="text-xs text-muted-foreground">
          {autoSendStepText(outlook, fire, fetchedAt)}
        </span>
      )}
    </li>
  )
}

/** What the next auto-send check would do: send a step, or why it waits. */
function autoSendCheckText(outlook: AutoSendOutlook): string {
  const overdue = outlook.catch_up
    ? ` It's overdue, so it runs once ${autoSendCheckWhen(outlook)}.`
    : ''
  return `${autoSendWaitText(outlook) ?? 'It sends the oldest due auto-send step.'}${overdue}`
}

// --- campaigns ----------------------------------------------------------------------------

/** How many campaigns the card lists before it says how many more there are. */
const CAMPAIGNS_SHOWN = 5

/**
 * The campaigns that are not archived (`GET /campaigns`, the list page's own query),
 * newest first: each one's name, status and who is enrolled, linked to its page.
 */
export function CampaignsCard() {
  const campaigns = useQuery(campaignsQuery)

  return (
    <GlanceCard title="Campaigns">
      {campaigns.isPending ? (
        <Checking />
      ) : campaigns.isError ? (
        <Failed what="Campaigns" />
      ) : campaigns.data.length === 0 ? (
        <Muted>No campaigns yet.</Muted>
      ) : (
        <>
          <ul className="flex flex-col gap-1.5">
            {campaigns.data.slice(0, CAMPAIGNS_SHOWN).map((campaign) => (
              <li key={campaign.id} className="flex flex-col">
                <span className="flex flex-wrap items-center gap-2">
                  <Link
                    to="/campaigns/$campaignId"
                    params={{ campaignId: String(campaign.id) }}
                    className="font-medium underline-offset-2 hover:underline"
                  >
                    {campaign.name}
                  </Link>
                  <CampaignStatusBadge status={campaign.status} />
                </span>
                <span className="text-xs text-muted-foreground">
                  {countsText(campaign.enrollments)}
                </span>
              </li>
            ))}
          </ul>
          <More
            shown={Math.min(campaigns.data.length, CAMPAIGNS_SHOWN)}
            total={campaigns.data.length}
          />
        </>
      )}
      <div>
        <Button size="sm" variant="ghost" render={<Link to="/campaigns" />}>
          All campaigns
        </Button>
      </div>
    </GlanceCard>
  )
}

// --- people -----------------------------------------------------------------------------

/**
 * Replies this week. Reply detection is P3-08 and is not built, so this says
 * so plainly, and shows the one count that does exist — inbound messages
 * logged in the last seven days — under its own name rather than as replies.
 */
export function RepliesCard({ level }: { level?: 2 | 3 }) {
  const inbound = useQuery(inboundQuery)

  return (
    <GlanceCard
      title="Replies this week"
      level={level}
      tone={inbound.data !== undefined && inbound.data.count > 0 ? 'ok' : undefined}
    >
      {inbound.isPending ? (
        <Checking />
      ) : inbound.isError ? (
        <Failed what="Inbound messages" />
      ) : (
        <>
          {!inbound.data.reply_detection && (
            <Muted>Reply detection is not set up yet, so campaign replies are not counted.</Muted>
          )}
          <p>
            {inbound.data.count === 0 ? (
              'No inbound messages logged in the last 7 days.'
            ) : (
              <>
                <span className="font-medium tabular-nums">{inbound.data.count}</span> inbound{' '}
                {inbound.data.count === 1 ? 'message' : 'messages'} (email and LinkedIn) logged in
                the last 7 days.
              </>
            )}
          </p>
        </>
      )}
    </GlanceCard>
  )
}

/**
 * Contacts netkeep saw change position recently: the best reason to reconnect
 * (spec 9.8). A change is one an enrichment visit noticed, dated when it was
 * noticed, not the start date on the profile, which people often fill in late (#286).
 */
export function ChangedJobsCard() {
  const changed = useQuery(changedJobsQuery)

  return (
    <GlanceCard
      title="Changed jobs"
      description={
        changed.isSuccess
          ? `New positions netkeep noticed in the last ${changed.data.days} days.`
          : undefined
      }
    >
      {changed.isPending ? (
        <Checking />
      ) : changed.isError ? (
        <Failed what="Changed jobs" />
      ) : changed.data.items.length === 0 ? (
        <Muted>netkeep noticed no position changes in the last {changed.data.days} days.</Muted>
      ) : (
        <>
          <ul className="flex flex-col gap-1.5">
            {changed.data.items.map((row) => (
              <li key={row.contact_id} className="flex flex-col">
                <span>
                  <Link
                    to="/contacts/$contactId"
                    params={{ contactId: String(row.contact_id) }}
                    className="font-medium underline-offset-2 hover:underline"
                  >
                    {row.contact_name || 'Unnamed contact'}
                  </Link>
                  <span className="text-muted-foreground">
                    {' '}
                    · noticed {formatDay(row.noticed_at)}
                  </span>
                </span>
                {(row.current_title !== null || row.current_company !== null) && (
                  <span className="text-xs text-muted-foreground">
                    {[row.current_title, row.current_company].filter(Boolean).join(' at ')}
                  </span>
                )}
              </li>
            ))}
          </ul>
          <More shown={changed.data.items.length} total={changed.data.total} />
        </>
      )}
    </GlanceCard>
  )
}
