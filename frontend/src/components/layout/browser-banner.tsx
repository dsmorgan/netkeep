import { Info, X } from 'lucide-react'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { detectBrowser } from '@/lib/browser'

export const BROWSER_BANNER_KEY = 'netkeeper.chromeBannerDismissed'

function wasDismissed(): boolean {
  try {
    return window.localStorage.getItem(BROWSER_BANNER_KEY) === '1'
  } catch {
    return false
  }
}

function remember(): void {
  try {
    window.localStorage.setItem(BROWSER_BANNER_KEY, '1')
  } catch {
    // Storage is blocked: the banner comes back on the next load, which is fine.
  }
}

/**
 * Tells you the UI is built and tested for Google Chrome (#549) when this tab is
 * another browser. It never blocks anything, and **Dismiss** is remembered per
 * browser. Detection only reads the browser's own identity; it never touches the
 * Chrome profile netkeeper uses for LinkedIn.
 */
export function BrowserBanner() {
  const [dismissed, setDismissed] = useState(wasDismissed)
  if (dismissed || detectBrowser(window.navigator) !== 'other') return null

  return (
    <div
      role="status"
      className="mb-4 flex flex-wrap items-start gap-3 rounded-xl border border-border bg-muted px-4 py-3 text-sm"
    >
      <Info className="mt-0.5 size-5 shrink-0 text-muted-foreground" aria-hidden="true" />
      <p className="min-w-0 flex-1">
        netkeeper is built and tested for Google Chrome. Other browsers may work, but they are not
        supported.
      </p>
      <Button
        size="sm"
        variant="outline"
        onClick={() => {
          remember()
          setDismissed(true)
        }}
      >
        <X aria-hidden="true" />
        Dismiss
      </Button>
    </div>
  )
}
