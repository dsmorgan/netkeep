import { AlertTriangle } from 'lucide-react'

import { Button } from '@/components/ui/button'

import type { Failed } from './read-failures'

/**
 * A read the confirm dialog needs failed (#829): what couldn't load, the server's
 * detail, and Try again. Confirm stays disabled meanwhile, so the dialog never lets a
 * move through with part of what it says missing; Cancel stays available.
 */
export function ReadFailure({
  text,
  onRetry,
  retrying,
}: {
  text: string
  onRetry: () => void
  retrying: boolean
}) {
  return (
    <div
      role="alert"
      className="flex flex-col items-start gap-2 rounded-lg bg-bad-soft px-3 py-2 text-destructive"
    >
      <p className="flex items-start gap-2">
        <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
        <span>{text}</span>
      </p>
      <Button size="sm" variant="outline" onClick={onRetry} disabled={retrying}>
        {retrying ? 'Trying again…' : 'Try again'}
      </Button>
    </div>
  )
}

/** Each failed read, in order. */
export function FailureList({ items }: { items: Failed[] }) {
  return items.map((item) => (
    <ReadFailure key={item.key} text={item.text} onRetry={item.retry} retrying={item.retrying} />
  ))
}
