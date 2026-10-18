import { useCallback, useEffect, useState, type ReactNode } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { CopyValue } from '@/features/settings/copy-button'

import { DISCONNECTED_EVENT, checkSession, type SessionInfo } from './api'

/** The query the server's connect link adds; the gate reads it once, then drops it. */
const CONNECT_PARAM = 'connect'

/** Whether the URL says a connect link had expired, removing the query either way. */
function takeExpiredFlag(): boolean {
  const url = new URL(window.location.href)
  const value = url.searchParams.get(CONNECT_PARAM)
  if (value === null) return false
  url.searchParams.delete(CONNECT_PARAM)
  window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}${url.hash}`)
  return value === 'expired'
}

/**
 * Renders the app once this browser is connected to the local API, and the
 * connect page when it isn't (#618, ADR 0013). The first browser connects on
 * its own; this shows nothing while it does. A 401 from any call later (the
 * cookie cleared, a new key) brings the check back. When the Keychain refused
 * the key, a banner above the app says so.
 */
export function SessionGate({ children }: { children: ReactNode }) {
  const [info, setInfo] = useState<SessionInfo | null>(null)
  const [expired] = useState(takeExpiredFlag)

  const check = useCallback(() => {
    void checkSession().then(setInfo)
  }, [])

  useEffect(() => {
    check()
    window.addEventListener(DISCONNECTED_EVENT, check)
    return () => window.removeEventListener(DISCONNECTED_EVENT, check)
  }, [check])

  if (info === null) return null
  if (info.status === 'disconnected') {
    return <ConnectPage info={info} expired={expired} onRetry={check} />
  }
  if (info.persisted) return children
  return (
    <>
      <KeyInMemoryBanner />
      {children}
    </>
  )
}

const KEYCHAIN_FIX =
  'To fix it, unlock your login keychain in Keychain Access, or allow netkeeper when macOS asks, then restart netkeeper.'

/** Shown above the app while the local API key lives in memory only. */
export function KeyInMemoryBanner() {
  return (
    <div role="status" className="border-b bg-muted px-4 py-2 text-sm">
      The Keychain refused netkeeper&apos;s local API key, so it&apos;s kept in memory until
      netkeeper stops. After a restart, the first browser to load netkeeper connects itself, and any
      other browser can&apos;t connect until the next restart. {KEYCHAIN_FIX}
    </div>
  )
}

/** What a browser that isn't connected sees: how to connect, never a dead end. */
export function ConnectPage({
  info,
  expired,
  onRetry,
}: {
  info: SessionInfo
  expired: boolean
  onRetry: () => void
}) {
  return (
    <main className="mx-auto grid max-w-xl gap-4 p-8">
      <h1 className="font-heading text-xl font-medium">netkeeper</h1>
      <Card>
        <CardHeader>
          <CardTitle level={2}>Connect this browser</CardTitle>
          <CardDescription>
            netkeeper only answers browsers you&apos;ve connected, so other programs on this Mac
            can&apos;t use it.
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3">
          {expired ? (
            <p role="alert">That link expired or was already used. Get a new one below.</p>
          ) : null}
          {info.claimedElsewhere ? (
            <p>
              Another browser or program has already connected to netkeeper, so this browser needs a
              link.
            </p>
          ) : null}
          {info.persisted ? (
            <>
              <p>
                Run this in Terminal, then open the link it prints in this browser. This browser
                then stays connected.
              </p>
              <CopyValue label="Command" value={info.openCommand} />
            </>
          ) : (
            <p>
              The Keychain refused netkeeper&apos;s local API key, so it&apos;s kept in memory and{' '}
              <code className="font-mono">netkeeper open</code> can&apos;t make a link. Restart
              netkeeper, then load this page first: the first browser to load it after a restart
              connects itself. {KEYCHAIN_FIX}
            </p>
          )}
          <div>
            <Button type="button" variant="outline" size="sm" onClick={onRetry}>
              Check again
            </Button>
          </div>
        </CardContent>
      </Card>
    </main>
  )
}
