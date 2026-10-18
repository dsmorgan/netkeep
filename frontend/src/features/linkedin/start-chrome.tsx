import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { toneClasses } from '@/components/ui/tone'
import { useServerEvent } from '@/features/events/use-server-event'
import { cn } from '@/lib/utils'

import { browserHealthQuery, linkedinKeys, startChrome } from './api'
import {
  LAUNCH_WAIT_MS,
  describeLaunch,
  describeRequestError,
  type LaunchEvent,
  type Said,
} from './start-chrome-copy'

/**
 * **Start Chrome** (#599): starts netkeeper's own Chrome (ADR 0012), on its dedicated
 * profile and debug port, with netkeeper in an app window, or opens that window in it
 * when it's running. It calls the one launcher (`POST /linkedin/browser/start`), which
 * refuses rather than touch any other Chrome, and says how it went from the
 * `browser.launch` event, refusals included, with what to do.
 *
 * Whether netkeeper's Chrome is running is `GET /linkedin/browser/health`'s reading of
 * its profile's lock file. The button is disabled while a run holds the browser and
 * while a launch is in flight, so one press is one launch. It isn't offered at all
 * when this process can't launch (not `serve`, or not the local owner). `compact`
 * (the dashboard) shows it only while netkeeper's Chrome isn't running.
 */
export function StartChrome({ compact = false }: { compact?: boolean }) {
  const queryClient = useQueryClient()
  const health = useQuery(browserHealthQuery)
  const [taskId, setTaskId] = useState<string | null>(null)
  const [said, setSaid] = useState<Said | null>(null)
  // A launch can end before the 202 is read: keep events for tasks this page hasn't
  // been told about yet, and match them once it has.
  const early = useRef(new Map<string, LaunchEvent>())

  const finish = (event: LaunchEvent) => {
    setTaskId(null)
    setSaid(describeLaunch(event))
    void queryClient.invalidateQueries({ queryKey: linkedinKeys.browserHealth() })
  }

  const start = useMutation({
    mutationFn: startChrome,
    onMutate: () => setSaid(null),
    onSuccess: ({ task_id }) => {
      const event = early.current.get(task_id)
      early.current.clear()
      if (event !== undefined) finish(event)
      else setTaskId(task_id)
    },
    onError: (error) => setSaid(describeRequestError(error)),
  })

  useServerEvent<LaunchEvent>('browser.launch', (event) => {
    if (taskId !== null && event.task_id === taskId) {
      finish(event)
      return
    }
    if (start.isPending) early.current.set(event.task_id, event)
    // Another launch (serve's at startup, another tab) changes what's running too.
    void queryClient.invalidateQueries({ queryKey: linkedinKeys.browserHealth() })
  })

  useEffect(() => {
    if (taskId === null) return
    const timer = setTimeout(() => {
      setTaskId(null)
      setSaid({
        tone: 'warn',
        text: "netkeeper didn't hear how the launch went. Look for netkeeper's Chrome window; if there's none, try again.",
      })
      void queryClient.invalidateQueries({ queryKey: linkedinKeys.browserHealth() })
    }, LAUNCH_WAIT_MS)
    return () => clearTimeout(timer)
  }, [taskId, queryClient])

  if (!health.isSuccess || !health.data.can_start_chrome) return null
  const { chrome_running: running, chrome_pid: pid, browser_held: held } = health.data
  if (compact && running && said === null) return null
  const inFlight = start.isPending || taskId !== null

  return (
    <section aria-label="Start Chrome" className="space-y-2">
      {!compact && (
        <p>
          {running
            ? `netkeeper's Chrome is running (pid ${pid ?? 'unknown'}). Start Chrome opens netkeeper in it again.`
            : "netkeeper's Chrome isn't running."}
        </p>
      )}
      {compact && !running && <p>netkeeper's Chrome isn't running.</p>}
      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          variant="outline"
          disabled={held || inFlight}
          onClick={() => start.mutate()}
        >
          {inFlight ? 'Starting Chrome…' : 'Start Chrome'}
        </Button>
        {held && !inFlight && (
          <span className="text-muted-foreground">
            A run is using the browser. Start Chrome when it ends.
          </span>
        )}
      </div>
      {said !== null && (
        <div
          role={said.tone === 'ok' ? 'status' : 'alert'}
          className={cn('nk-glass space-y-1 p-3', toneClasses(said.tone))}
        >
          <p>{said.text}</p>
          {said.detail !== undefined && said.detail !== '' && (
            <p className="text-xs text-muted-foreground">netkeeper said: {said.detail}</p>
          )}
        </div>
      )}
    </section>
  )
}
