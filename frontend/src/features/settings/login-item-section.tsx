import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useId } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'

import { loginItemQuery, repairLoginItem, setLoginItem } from './api'

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * Run at login (#600): a switch that installs or removes the launchd agent that starts
 * `netkeeper serve`. It shows the state the server read from the plist and `launchctl`,
 * not a remembered flag, and it is disabled with the reason off macOS. Turning it on
 * while this server runs registers the agent for the next login rather than starting a
 * second copy; the answer's message says so.
 */
export function LoginItemSection() {
  const id = useId()
  const state = useQuery(loginItemQuery)
  const queryClient = useQueryClient()
  const change = useMutation({
    mutationFn: setLoginItem,
    onSuccess: (data) => queryClient.setQueryData(loginItemQuery.queryKey, data),
  })
  const repair = useMutation({
    mutationFn: repairLoginItem,
    onSuccess: (data) => queryClient.setQueryData(loginItemQuery.queryKey, data),
  })
  const item = state.data

  return (
    <Card size="sm">
      <CardHeader>
        <h2 className="font-heading text-sm leading-snug font-medium">Run at login</h2>
        <CardDescription>
          netkeeper keeps running in the background and restarts after a crash. This adds a launchd
          agent in your Library folder; turning it off removes the agent and keeps your data.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {state.isPending && <p role="status">Loading…</p>}
        {state.isError && <p role="alert">{message(state.error)}</p>}
        {item && (
          <>
            <div className="flex items-center gap-2">
              <Checkbox
                id={id}
                checked={item.enabled}
                disabled={!item.supported || change.isPending}
                onCheckedChange={(checked) => {
                  change.reset()
                  change.mutate(checked === true)
                }}
              />
              <Label htmlFor={id}>Start netkeeper when I log in</Label>
            </div>
            {!item.supported && item.unsupported_reason && (
              <p className="text-muted-foreground">{item.unsupported_reason}</p>
            )}
            {item.supported && item.enabled && !item.running_under_launchd && (
              <p className="text-muted-foreground">
                On, but not running under launchd yet: it starts at your next login.
              </p>
            )}
            {item.legacy && (
              <p role="alert">
                An agent from the old install script serves this data directory, and it can no
                longer start because that script is gone.
              </p>
            )}
            {item.points_elsewhere && (
              <p role="alert">
                The agent runs{' '}
                <code className="font-mono text-xs break-all">{item.points_elsewhere}</code>, not
                this copy of netkeeper.
              </p>
            )}
            {(item.legacy || item.points_elsewhere) && (
              <Button
                size="sm"
                disabled={repair.isPending}
                onClick={() => {
                  repair.reset()
                  repair.mutate()
                }}
              >
                {repair.isPending ? 'Repairing…' : 'Repair'}
              </Button>
            )}
            {repair.isSuccess && repair.data.message && <p role="status">{repair.data.message}</p>}
            {repair.isError && <p role="alert">{message(repair.error)}</p>}
            {change.isSuccess && change.data.message && <p role="status">{change.data.message}</p>}
            {change.isError && <p role="alert">{message(change.error)}</p>}
            {item.supported && (
              <p className="text-muted-foreground">
                Log: <code className="font-mono text-xs break-all">{item.log_path}</code>
              </p>
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}
