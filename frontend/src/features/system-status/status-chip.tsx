import { Link } from '@tanstack/react-router'
import { Check, Circle, CircleX, LoaderCircle, TriangleAlert } from 'lucide-react'
import { useState, type ReactNode } from 'react'

import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTitle, PopoverTrigger } from '@/components/ui/popover'
import { cn } from '@/lib/utils'

import { TONE_TEXT, type StatusTone, type SystemStatus } from './roll-up'
import { TONE_CHIP_CLASSES } from './tone-classes'

const INK: Record<StatusTone, string> = {
  ok: 'text-ok-ink',
  busy: 'text-accent-ink',
  warn: 'text-warn-ink',
  bad: 'text-bad-ink',
  off: 'text-muted-foreground',
}

/**
 * Each tone's icon, distinct in shape and not only in color: a check, a spinner (still
 * under reduced motion), a triangle, a crossed circle and an empty ring.
 */
export function ToneIcon({ tone, className }: { tone: StatusTone; className?: string }) {
  const props = {
    'aria-hidden': true,
    'data-icon': tone,
    className: cn('size-3.5 shrink-0', className),
  }
  switch (tone) {
    case 'ok':
      return <Check {...props} strokeWidth={2.75} />
    case 'busy':
      return (
        <LoaderCircle
          {...props}
          strokeWidth={2.5}
          className={cn(props.className, 'animate-spin motion-reduce:animate-none')}
        />
      )
    case 'warn':
      return <TriangleAlert {...props} strokeWidth={2.5} />
    case 'bad':
      return <CircleX {...props} strokeWidth={2.5} />
    case 'off':
      return <Circle {...props} strokeWidth={2.5} />
  }
}

/**
 * One top-bar status chip (#751): "<System> · <word>" and a muted fact, a button that
 * opens the popover with one row per part, when it was updated, and one button to the
 * page with the details or the fix (primary when the chip needs you). Below `sm` the
 * chip shows only its icon and system name; the word and fact stay for screen readers.
 * The popover closes on Escape and gives focus back to the chip.
 */
export function StatusChip({
  status,
  updated,
  children,
}: {
  status: SystemStatus
  /** "Updated 3 min ago". */
  updated: string
  /** More controls for the popover, between the rows and the footer. */
  children?: ReactNode
}) {
  const [open, setOpen] = useState(false)
  const urgent = status.tone === 'warn' || status.tone === 'bad'
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        data-system={status.system}
        data-tone={status.tone}
        aria-haspopup="dialog"
        className={cn(
          'inline-flex min-h-7 max-w-full min-w-0 items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-semibold whitespace-nowrap outline-none hover:border-muted-foreground focus-visible:ring-2 focus-visible:ring-ring data-popup-open:ring-2 data-popup-open:ring-ring',
          TONE_CHIP_CLASSES[status.tone],
        )}
      >
        <ToneIcon tone={status.tone} />
        <span className="shrink-0">{status.name}</span>
        <span className="truncate max-sm:sr-only"> · {status.word}</span>
        {status.fact !== '' && (
          <span className="truncate font-medium max-sm:sr-only">
            <span className="sr-only">, </span>
            {status.fact}
          </span>
        )}
      </PopoverTrigger>
      <PopoverContent className="flex w-90 flex-col gap-2.5 p-3.5">
        <PopoverTitle className="flex items-center gap-2 font-heading text-base font-extrabold">
          <span
            className={cn(
              'inline-flex items-center gap-1.5 rounded-full border px-2 py-px text-xs font-semibold',
              TONE_CHIP_CLASSES[status.tone],
            )}
          >
            <ToneIcon tone={status.tone} />
            {status.word}
          </span>
          {status.name}
        </PopoverTitle>
        <ul aria-label={`${status.name} status`} className="flex flex-col">
          {status.parts.map((part) => (
            <li
              key={part.key}
              data-part={part.key}
              data-tone={part.tone}
              className="grid grid-cols-[1rem_minmax(0,1fr)] gap-x-2.5 gap-y-0.5 border-t border-border py-1.5 first:border-t-0"
            >
              <ToneIcon tone={part.tone} className={cn('mt-0.5', INK[part.tone])} />
              <span className="font-semibold">
                <span className="sr-only">{TONE_TEXT[part.tone]}: </span>
                {part.label}
              </span>
              <span className="col-start-2 text-xs text-muted-foreground">{part.detail}</span>
            </li>
          ))}
        </ul>
        {children}
        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border pt-2.5">
          <span className="text-xs text-muted-foreground">{updated}</span>
          {status.reload === true ? (
            // No page loads while the server is down; a reload picks up once it is back.
            <Button size="sm" onClick={() => window.location.reload()}>
              Reload
            </Button>
          ) : (
            <Button
              size="sm"
              variant={urgent ? 'default' : 'outline'}
              render={<Link to={status.target.to} hash={status.target.hash} />}
              onClick={() => setOpen(false)}
            >
              {status.tone === 'bad' ? `Fix on the ${status.page} page` : `Open ${status.page}`}
            </Button>
          )}
        </div>
      </PopoverContent>
    </Popover>
  )
}
