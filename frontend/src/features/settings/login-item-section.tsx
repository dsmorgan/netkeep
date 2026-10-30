import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useId } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'

import {
  buildLauncherApp,
  configSettingsQuery,
  loginItemQuery,
  repairLoginItem,
  saveConfigSettings,
  setLoginItem,
} from './api'

/** The Settings value that decides whether the menu-bar job runs beside `serve` (#763). */
export const MENUBAR_ICON_KEY = 'menubar.show_icon'

/** The check steps, on the project's site, since the app serves no docs of its own. */
export const LOGIN_ITEM_TROUBLESHOOTING_URL =
  'https://github.com/dsmorgan/netkeep/blob/main/docs/guide/troubleshooting.md#run-at-login-does-nothing'

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * Run at login (#600): a switch that installs or removes the launchd agent that starts
 * `netkeep serve`. It shows the state the server read from the plist and `launchctl`,
 * not a remembered flag, and it is disabled with the reason off macOS. Turning it on
 * while this server runs registers the agent for the next login rather than starting a
 * second copy; the answer's message says so.
 *
 * On macOS, the Show menu-bar icon switch (#763) follows it, and below that the launcher
 * app (#754): whether `~/Applications/netkeep.app` exists and which netkeep it runs, with a
 * button that builds or rebuilds it.
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
  const build = useMutation({
    mutationFn: buildLauncherApp,
    onSuccess: (data) => queryClient.setQueryData(loginItemQuery.queryKey, data),
  })
  const item = state.data

  return (
    <Card size="sm">
      <CardHeader>
        <h2 className="font-heading text-sm leading-snug font-bold">Run at login</h2>
        <CardDescription>
          netkeep keeps running in the background and restarts after a crash. This adds a launchd
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
              <Label htmlFor={id}>Start netkeep when I log in</Label>
            </div>
            {!item.supported && item.unsupported_reason && (
              <p className="text-muted-foreground">{item.unsupported_reason}</p>
            )}
            {item.supported && (
              <p>
                State: <strong>{item.state}</strong>
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
                this copy of netkeep.
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
            {item.supported && item.enabled && (
              <p className="text-muted-foreground">
                How to check: after your next login, open netkeep&apos;s bookmark without starting
                anything.{' '}
                <a
                  href={LOGIN_ITEM_TROUBLESHOOTING_URL}
                  target="_blank"
                  rel="noreferrer"
                  className="underline underline-offset-2"
                >
                  If the page doesn't open
                </a>
                , the troubleshooting entry shows how to ask launchd about the agent.
              </p>
            )}
            {item.supported && (
              <p className="text-muted-foreground">
                Agent label: <code className="font-mono text-xs break-all">{item.label}</code>
                <br />
                Log: <code className="font-mono text-xs break-all">{item.log_path}</code>
              </p>
            )}
            {item.supported && <MenuBarIconSwitch />}
            {item.launcher.supported && (
              <div className="space-y-2 border-t pt-3">
                <h3 className="font-heading font-bold">Launcher app</h3>
                <p className="text-muted-foreground">
                  An icon in your Applications folder that opens netkeep from the Dock, Spotlight or
                  Launchpad, starting it first if it isn&apos;t running.
                </p>
                <p>
                  Launcher: <strong>{item.launcher.state}</strong>
                  <br />
                  <code className="font-mono text-xs break-all">{item.launcher.path}</code>
                </p>
                {item.launcher.runs && (
                  <p className="text-muted-foreground">
                    It runs{' '}
                    <code className="font-mono text-xs break-all">{item.launcher.runs}</code>.
                  </p>
                )}
                <Button
                  size="sm"
                  variant="outline"
                  disabled={build.isPending}
                  onClick={() => {
                    build.reset()
                    build.mutate()
                  }}
                >
                  {build.isPending ? 'Building…' : item.launcher.exists ? 'Rebuild' : 'Build'}
                </Button>
                {build.isSuccess && build.data.message && <p role="status">{build.data.message}</p>}
                {build.isError && <p role="alert">{message(build.error)}</p>}
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}

/**
 * Show menu-bar icon (#763; ADR 0014, rules 8 and 9): a Settings value like the config
 * cards' ones, shown here because it decides the login item's second job. It saves on its
 * own, with the backend's words for what it does; a value config.toml sets shows locked,
 * with the file's path. The card shows it on macOS only: elsewhere there is no menu bar.
 */
function MenuBarIconSwitch() {
  const id = useId()
  const current = useQuery(configSettingsQuery)
  const queryClient = useQueryClient()
  const save = useMutation({
    mutationFn: (on: boolean) => saveConfigSettings({ [MENUBAR_ICON_KEY]: on }),
    onSuccess: (data) => queryClient.setQueryData(configSettingsQuery.queryKey, data),
  })
  if (current.isPending) return null
  if (current.isError) return <p role="alert">{message(current.error)}</p>
  const field = current.data.fields.find((f) => f.key === MENUBAR_ICON_KEY)
  if (field === undefined) return null
  return (
    <div className="space-y-1.5 border-t pt-3" role="group" aria-label={field.label}>
      <div className="flex items-center gap-2">
        <Checkbox
          id={id}
          checked={field.value === true}
          disabled={!field.editable || save.isPending}
          onCheckedChange={(checked) => {
            save.reset()
            save.mutate(checked === true)
          }}
        />
        <Label htmlFor={id}>{field.label}</Label>
      </div>
      <p className="text-muted-foreground">
        {field.help} {field.applies_note}
      </p>
      {field.locked_reason !== null && (
        <p className="text-muted-foreground">{field.locked_reason}</p>
      )}
      {save.isError && <p role="alert">{message(save.error)}</p>}
    </div>
  )
}
