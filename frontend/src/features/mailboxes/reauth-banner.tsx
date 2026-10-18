import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle } from 'lucide-react'
import { useCallback } from 'react'

import { Button } from '@/components/ui/button'
import { toneClasses } from '@/components/ui/tone'
import { useServerEvent } from '@/features/events/use-server-event'
import { cn } from '@/lib/utils'

import { mailboxKeys, mailboxListQuery, navigation, reasonText, startAuthorization } from './api'

/**
 * The app-wide banner for a mailbox that needs authorizing again (spec 11.5).
 *
 * Shown on every page while any mailbox is `reauth_required`: email steps are
 * paused until someone does. The backend's poll publishes `mailbox.status` the
 * moment it marks one, so the banner appears without a reload. It reads the
 * mailboxes alone, never the Keychain, so it shows even while the Keychain is locked.
 */
export function ReauthBanner() {
  const queryClient = useQueryClient()
  const mailboxes = useQuery(mailboxListQuery)
  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: mailboxKeys.all })
  }, [queryClient])
  useServerEvent('mailbox.status', refresh)

  const reauthorize = useMutation({
    mutationFn: startAuthorization,
    onSuccess: (url) => navigation.assign(url),
  })

  const stuck = mailboxes.data?.filter((mailbox) => mailbox.status === 'reauth_required') ?? []
  const first = stuck[0]
  if (first === undefined) return null

  return (
    <div
      role="alert"
      className={cn('nk-glass mb-4 flex flex-wrap items-start gap-3 px-4 py-3', toneClasses('bad'))}
    >
      <AlertTriangle className="mt-0.5 size-5 shrink-0 text-bad-ink" aria-hidden="true" />
      <div className="min-w-0 flex-1 space-y-1">
        <h2 className="font-heading text-sm font-bold">Gmail needs you to sign in again</h2>
        {stuck.map((mailbox) => (
          <p key={mailbox.id} className="text-sm">
            <span className="font-medium">{mailbox.email}</span>:{' '}
            {reasonText(mailbox.status_reason) ?? 'Google no longer accepts the token.'}
          </p>
        ))}
        <p className="text-xs text-muted-foreground">
          Email steps are paused until it is authorized again.
        </p>
        {reauthorize.isError && <p className="text-sm">{(reauthorize.error as Error).message}</p>}
      </div>
      <Button
        size="sm"
        onClick={() => reauthorize.mutate(first.id)}
        disabled={reauthorize.isPending}
      >
        {reauthorize.isPending ? 'Opening Google…' : 'Re-authorize'}
      </Button>
    </div>
  )
}
