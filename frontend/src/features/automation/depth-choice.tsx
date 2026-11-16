import { Radio } from '@base-ui/react/radio'
import { RadioGroup } from '@base-ui/react/radio-group'
import { useId } from 'react'

import { toneClasses } from '@/components/ui/tone'
import { cn } from '@/lib/utils'

import { DEPTH_LABEL, type Depth } from './api'

const DEPTH_LINE: Record<Depth, string> = {
  quick:
    'New connections and the LinkedIn inbox. You run a full sync yourself when you want removed connections noticed.',
  full: 'Adds the weekly full sync and profile refreshes for stale contacts. More time on LinkedIn each week.',
}

/**
 * LinkedIn's How thorough choice (#829): Quick checks, or Quick checks and a weekly full
 * sync. Settings shows it under the track with a line each, in a flat green tint; the
 * dialog that turns on Automatic shows it `compact`, labels only, so the depth is chosen
 * before anything starts.
 */
export function DepthChoice({
  value,
  onChoose,
  busy = false,
  compact = false,
}: {
  value: Depth
  onChoose: (depth: Depth) => void
  busy?: boolean
  compact?: boolean
}) {
  const id = useId()
  return (
    <div
      className={cn(
        'flex flex-col gap-2',
        !compact && cn(toneClasses('ok'), 'nk-flat rounded-lg px-4 py-3.5'),
      )}
    >
      <p
        id={id}
        className={compact ? 'font-semibold text-foreground' : 'text-sm font-extrabold text-ok-ink'}
      >
        How thorough
      </p>
      <RadioGroup
        aria-labelledby={id}
        aria-busy={busy || undefined}
        value={value}
        readOnly={busy}
        onValueChange={(next) => onChoose(next as Depth)}
        className={cn('flex flex-col', compact ? 'gap-0.5' : 'gap-1')}
      >
        {(['quick', 'full'] as const).map((depth) => (
          <Radio.Root
            key={depth}
            value={depth}
            aria-labelledby={`${id}-${depth}`}
            aria-describedby={compact ? undefined : `${id}-${depth}-line`}
            className={cn(
              'group flex cursor-pointer items-start gap-2.5 rounded-md py-1 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring',
              compact ? 'min-h-8 items-center' : 'min-h-11',
            )}
          >
            <span
              aria-hidden="true"
              className={cn(
                'inline-flex size-4.5 shrink-0 items-center justify-center rounded-full border-2 border-muted-foreground group-data-checked:border-ok-ink',
                !compact && 'mt-0.5',
              )}
            >
              <span className="size-2 rounded-full group-data-checked:bg-ok-ink" />
            </span>
            <span className="flex flex-col gap-0.5">
              <span
                id={`${id}-${depth}`}
                className={cn('text-sm', compact ? 'font-semibold text-foreground' : 'font-bold')}
              >
                {DEPTH_LABEL[depth]}
              </span>
              {!compact && (
                <span id={`${id}-${depth}-line`} className="text-xs text-muted-foreground">
                  {DEPTH_LINE[depth]}
                </span>
              )}
            </span>
          </Radio.Root>
        ))}
      </RadioGroup>
    </div>
  )
}
