import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { pollStatusKeys } from '@/features/poll-status/api'

import { linkedinKeys, pauseSchedule, unpauseSchedule } from './api'
import { formatWhen } from './fields'
import type { Schedule } from './types'

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * Pause and unpause the schedule (#324), for the dashboard and the LinkedIn page.
 *
 * Pausing holds new scheduled runs without disarming: a run already going keeps
 * going (Cancel stops it), and the pause survives a restart, so it doesn't ask first.
 * Unpausing lets scheduled runs reach LinkedIn on their own again, so it asks first
 * (#829). It replays nothing that was skipped: each kind waits for its next due time,
 * and the line under the button says so.
 */
export function SchedulePause({ schedule }: { schedule: Schedule }) {
  const queryClient = useQueryClient()
  const onSuccess = (data: Schedule) => {
    queryClient.setQueryData(linkedinKeys.schedule(), data)
    void queryClient.invalidateQueries({ queryKey: linkedinKeys.status() })
    void queryClient.invalidateQueries({ queryKey: pollStatusKeys.all })
  }
  const pause = useMutation({ mutationFn: pauseSchedule, onSuccess })
  const unpause = useMutation({ mutationFn: unpauseSchedule, onSuccess })
  const [asking, setAsking] = useState(false)
  const failed = pause.error ?? (asking ? null : unpause.error)

  return (
    <div className="flex flex-col gap-2">
      {schedule.paused ? (
        <>
          <p role="status" className="font-medium text-warn-ink">
            Paused{schedule.paused_at === null ? '' : ` since ${formatWhen(schedule.paused_at)}`} —
            no new scheduled run starts.
          </p>
          <p className="text-muted-foreground">
            A run already going keeps going. When you unpause, nothing skipped is replayed: each
            kind runs at its next due time.
          </p>
          <div>
            <Button
              size="sm"
              variant="outline"
              onClick={() => setAsking(true)}
              disabled={unpause.isPending}
            >
              {unpause.isPending ? 'Unpausing…' : 'Unpause schedule'}
            </Button>
          </div>
          <ConfirmDialog
            open={asking}
            onOpenChange={setAsking}
            title="Unpause scheduled runs?"
            confirmVariant="default"
            confirmLabel="Unpause schedule"
            pending={unpause.isPending}
            error={unpause.isError ? message(unpause.error) : null}
            onConfirm={() => unpause.mutateAsync().then(() => setAsking(false))}
          >
            <p>
              Scheduled syncs and enrichment will start contacting LinkedIn on their own again,
              within active hours, without you starting or watching each one.
            </p>
          </ConfirmDialog>
        </>
      ) : (
        <div>
          <Button
            size="sm"
            variant="outline"
            onClick={() => pause.mutate()}
            disabled={pause.isPending}
          >
            {pause.isPending ? 'Pausing…' : 'Pause schedule'}
          </Button>
        </div>
      )}
      {failed !== null && <p role="alert">{message(failed)}</p>}
    </div>
  )
}
