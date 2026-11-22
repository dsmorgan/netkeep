import { useQuery } from '@tanstack/react-query'

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { automationQuery, FULL_UPKEEP_KINDS } from '@/features/automation/api'

import { scheduleQuery } from './api'
import { formatWhen } from './fields'
import { RUN_KIND_LABELS, type RunKind } from './types'

/** The card's line off Automatic, in the switch's own words. */
const NOTHING_SCHEDULED = {
  off: 'Nothing runs on its own while LinkedIn is Off.',
  manual: 'Nothing runs on its own while LinkedIn is on Manual.',
} as const

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * What runs on its own (spec 9.4): on Automatic, each kind's next due time; otherwise a
 * line that nothing is scheduled. The switch itself is the page's automation bar
 * (#835), so this card only reads the position and never contradicts it.
 */
export function ScheduleCard() {
  const automation = useQuery(automationQuery)
  const schedule = useQuery(scheduleQuery)
  const state = automation.data?.linkedin

  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle level={2}>Scheduled runs</CardTitle>
        <CardDescription>Syncs and enrichment that run on their own, on Automatic.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {state === undefined && !automation.isError && <p role="status">Loading…</p>}
        {state === undefined && automation.isError && (
          <p role="alert">{message(automation.error)}</p>
        )}
        {state !== undefined && state.position !== 'automatic' && (
          <p className="text-muted-foreground">{NOTHING_SCHEDULED[state.position]}</p>
        )}
        {state?.position === 'automatic' && schedule.isError && (
          <p role="alert">{message(schedule.error)}</p>
        )}
        {state?.position === 'automatic' && schedule.isSuccess && schedule.data.jobs.length > 0 && (
          <ul aria-label="Next runs" className="space-y-1 text-muted-foreground">
            {schedule.data.jobs.map((job) => (
              <li key={job.kind}>
                {RUN_KIND_LABELS[job.kind as RunKind] ?? job.kind}:{' '}
                {state.depth === 'quick' && FULL_UPKEEP_KINDS.has(job.kind)
                  ? 'not on quick checks'
                  : `every ${job.interval_hours}h, next due ${
                      job.next_due === null ? 'not scheduled' : formatWhen(job.next_due)
                    }`}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}
