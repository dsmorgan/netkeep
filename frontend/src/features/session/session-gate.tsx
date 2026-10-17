import { useCallback, useEffect, useState, type ReactNode } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { CopyValue } from '@/features/settings/copy-button'

import { DISCONNECTED_EVENT, checkSession, type SessionState } from './api'

/** The query the server's connect link adds when its code was spent or stale. */
const EXPIRED_PARAM = 'connect'

function linkExpired(): boolean {
  return new URLSearchParams(window.location.search).get(EXPIRED_PARAM) === 'expired'
}

/**
 * Renders the app once this browser is connected to the local API, and the
 * connect page when it isn't (#618, ADR 0013). The first browser connects on
 * its own; this shows nothing while it does. A 401 from any call later (the
 * cookie cleared, a new key) brings the check back.
 */
export function SessionGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<SessionState | 'checking'>('checking')

  const check = useCallback(() => {
    void checkSession().then(setState)
  }, [])

  useEffect(() => {
    check()
    window.addEventListener(DISCONNECTED_EVENT, check)
    return () => window.removeEventListener(DISCONNECTED_EVENT, check)
  }, [check])

  if (state === 'checking') return null
  if (state === 'connected') return children
  return <ConnectPage onRetry={check} expired={linkExpired()} />
}

/** What a browser that isn't connected sees: how to connect, never a dead end. */
export function ConnectPage({ onRetry, expired }: { onRetry: () => void; expired: boolean }) {
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
          <p>
            Run this in Terminal, then open the link it prints in this browser. This browser then
            stays connected.
          </p>
          <CopyValue label="Command" value="netkeeper open" />
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
