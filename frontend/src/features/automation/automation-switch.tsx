import { Radio } from '@base-ui/react/radio'
import { RadioGroup } from '@base-ui/react/radio-group'
import { useId } from 'react'

import { cn } from '@/lib/utils'

import { POSITION_LABEL, POSITIONS, type Position } from './api'

/**
 * Each position's state color, from the B5 tokens: Off is muted, Manual the accent,
 * Automatic green. The selected position colors the track up to it.
 */
const TONE: Record<Position, { fill: string; border: string; ink: string; halo: string }> = {
  off: {
    fill: 'bg-muted-foreground',
    border: 'border-muted-foreground',
    ink: 'text-foreground',
    halo: 'shadow-[0_0_0_5px_color-mix(in_oklab,var(--muted-foreground)_22%,transparent)]',
  },
  manual: {
    fill: 'bg-primary',
    border: 'border-primary',
    ink: 'text-accent-ink',
    halo: 'shadow-[0_0_0_5px_color-mix(in_oklab,var(--primary)_22%,transparent)]',
  },
  automatic: {
    fill: 'bg-ok',
    border: 'border-ok',
    ink: 'text-ok-ink',
    halo: 'shadow-[0_0_0_5px_color-mix(in_oklab,var(--ok)_22%,transparent)]',
  },
}

/** How far the track fills for each position, of the span between the first and last stop. */
const FILL_WIDTH: Record<Position, string> = {
  off: 'w-0',
  manual: 'w-[33.34%]',
  automatic: 'w-[66.68%]',
}

interface SwitchProps {
  /** The group's accessible name, such as "LinkedIn automation". */
  label: string
  value: Position
  onChoose: (position: Position) => void
  /** Stops that can't be chosen now; the reason is the caller's to show and link. */
  disabled?: Partial<Record<Position, boolean>>
  /** The id of the text that says why a stop is disabled, for the disabled stop. */
  disabledReasonId?: string
  /** While a move is sending: the group stays focusable but takes no new choice. */
  busy?: boolean
}

/**
 * The Settings page's three-stop track (#829, mockup C): Off, Manual and Automatic,
 * each with its one-line description. A real radio group: Tab reaches the chosen
 * stop, the arrow keys move between stops, and each stop shows the focus ring.
 * Choosing a stop only calls `onChoose`; what is checked is always the server's
 * position, so a move that asks first leaves the track where it was until confirmed.
 */
export function AutomationLadder({
  label,
  value,
  onChoose,
  lines,
  disabled = {},
  disabledReasonId,
  busy = false,
}: SwitchProps & { lines: Record<Position, string> }) {
  const id = useId()
  const tone = TONE[value]
  const selected = POSITIONS.indexOf(value)
  return (
    <RadioGroup
      aria-label={label}
      aria-busy={busy || undefined}
      value={value}
      readOnly={busy}
      onValueChange={(next) => onChoose(next as Position)}
      className="relative grid grid-cols-3 items-start"
    >
      <span
        aria-hidden="true"
        className="absolute top-[13px] right-[16.66%] left-[16.66%] h-1 rounded-full bg-border"
      />
      <span
        aria-hidden="true"
        className={cn(
          'absolute top-[13px] left-[16.66%] h-1 rounded-full transition-[width] motion-reduce:transition-none',
          tone.fill,
          FILL_WIDTH[value],
        )}
      />
      {POSITIONS.map((position, index) => {
        const on = position === value
        const off = disabled[position] === true
        return (
          <Radio.Root
            key={position}
            value={position}
            disabled={off}
            aria-labelledby={`${id}-${position}`}
            aria-describedby={[`${id}-${position}-line`, off ? disabledReasonId : undefined]
              .filter(Boolean)
              .join(' ')}
            data-position={position}
            className={cn(
              'relative flex min-h-11 cursor-pointer flex-col items-center gap-2 rounded-lg px-2 pb-1 text-center outline-none',
              'focus-visible:ring-2 focus-visible:ring-ring',
              'data-disabled:cursor-not-allowed data-disabled:opacity-50',
            )}
          >
            <span
              aria-hidden="true"
              className={cn(
                'box-border size-7.5 rounded-full border-2 border-border bg-card transition-colors motion-reduce:transition-none',
                index < selected && tone.border,
                on && cn('border-transparent', tone.fill, tone.halo),
              )}
            />
            <span
              id={`${id}-${position}`}
              className={cn('text-sm font-semibold', on && cn('font-extrabold', tone.ink))}
            >
              {POSITION_LABEL[position]}
            </span>
            <span id={`${id}-${position}-line`} className="text-xs text-muted-foreground">
              {lines[position]}
            </span>
          </Radio.Root>
        )
      })}
    </RadioGroup>
  )
}

/**
 * The compact switch for the status pill popovers and the LinkedIn page (#829, mockup
 * D): the same three positions as a segmented radio group, with the same keyboard.
 */
export function AutomationSegments({
  label,
  value,
  onChoose,
  disabled = {},
  disabledReasonId,
  busy = false,
}: SwitchProps) {
  return (
    <RadioGroup
      aria-label={label}
      aria-busy={busy || undefined}
      value={value}
      readOnly={busy}
      onValueChange={(next) => onChoose(next as Position)}
      className="flex gap-0.5 rounded-[9px] bg-muted p-[3px] shadow-[inset_0_0_0_1px_var(--border)]"
    >
      {POSITIONS.map((position) => {
        const off = disabled[position] === true
        return (
          <Radio.Root
            key={position}
            value={position}
            disabled={off}
            aria-describedby={off ? disabledReasonId : undefined}
            data-position={position}
            className={cn(
              'inline-flex min-h-8 flex-1 cursor-pointer items-center justify-center gap-1.5 rounded-[7px] px-2.5 text-[13px] font-semibold text-muted-foreground outline-none',
              'focus-visible:ring-2 focus-visible:ring-ring',
              'data-checked:bg-card data-checked:font-bold data-checked:shadow-[0_0_0_1px_var(--border),0_1px_3px_rgb(16_20_42/18%)]',
              position === value && TONE[position].ink,
              'data-disabled:cursor-not-allowed data-disabled:opacity-50',
            )}
          >
            <span
              aria-hidden="true"
              className={cn('size-2 shrink-0 rounded-full', TONE[position].fill)}
            />
            {POSITION_LABEL[position]}
          </Radio.Root>
        )
      })}
    </RadioGroup>
  )
}
