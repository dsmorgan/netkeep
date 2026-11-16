import { useQuery } from '@tanstack/react-query'

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ActivityIndicator } from '@/features/attention/activity-indicator'
import { FULL_UPKEEP_KINDS } from '@/features/automation/api'
import { LinkedInSwitchCompact } from '@/features/automation/automation-compact'
import { useLinkedInAutomation } from '@/features/automation/use-automation'

import { scheduleQuery } from './api'
import { formatWhen } from './fields'
import { RUN_KIND_LABELS, type RunKind } from './types'

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

const NOT_AUTOMATIC = {
  off: 'Off: netkeep leaves LinkedIn alone.',
  manual: 'Manual: nothing runs on its own. Runs start when you click Start.',
} as const

/**
 * What runs on its own (spec 9.4), always shown plainly: the LinkedIn automation
 * switch (#829), the same one Settings › Automation and the LinkedIn pill have, and,
 * on Automatic, each kind's next due time. Moving to Automatic, or adding the weekly
 * full sync, asks first and sends `confirm: true`; moving back is one click.
 */
export function ScheduleCard() {
  const schedule = useQuery(scheduleQuery)
  const control = useLinkedInAutomation()
  const state = control.state

  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle level={2}>Scheduled runs</CardTitle>
        <CardDescription>Syncs and enrichment that run on their own, on Automatic.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <ActivityIndicator />
        {state === undefined && control.loadError === null && <p role="status">Loading…</p>}
        <LinkedInSwitchCompact control={control} />
        {state !== undefined && state.position !== 'automatic' && (
          <p className="text-muted-foreground">{NOT_AUTOMATIC[state.position]}</p>
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
      {control.dialog}
    </Card>
  )
}
