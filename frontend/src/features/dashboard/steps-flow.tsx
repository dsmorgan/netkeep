import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { useCallback, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { crmSearch } from '@/features/crm/crm-tabs'
import { useServerEvent } from '@/features/events/use-server-event'
import { cn } from '@/lib/utils'

import { setSkipped, setupKeys, setupQuery, type Setup, type SetupStep } from './setup-api'
import {
  BOX_TONE,
  TARGET_LABELS,
  boxLook,
  layoutSteps,
  needsText,
  stateLabel,
  type BoxLook,
  type Layout,
} from './steps-layout'

/**
 * The setup map (#667): the setup flow as a diagram, one box per step, with an arrow
 * from each prerequisite. Every state comes from `GET /setup` (#651); a box links to
 * the page that does its step and never starts anything there.
 *
 * The boxes are an ordered list in reading order (column by column), so a screen
 * reader hears each step with its state and the tab order follows the diagram. The
 * arrows are drawn in an SVG over the grid, hidden from assistive tech, and the
 * narrow layout is one column without them: each box that waits says what for.
 */
export function StepsFlow() {
  const queryClient = useQueryClient()
  const setup = useQuery(setupQuery)
  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: setupKeys.all })
  }, [queryClient])
  // A sync or an inbox read finishing can complete a step.
  useServerEvent('run.finished', refresh)
  useServerEvent('mailbox.status', refresh)
  const skip = useMutation({
    mutationFn: setSkipped,
    onSuccess: (data) => queryClient.setQueryData(setupQuery.queryKey, data),
  })
  // Stable while the query's data is, so the diagram measures only when steps change.
  const layout = useMemo(
    () => (setup.data === undefined ? undefined : layoutSteps(setup.data.steps)),
    [setup.data],
  )

  if (setup.isPending) {
    return (
      <p role="status" className="text-muted-foreground">
        Checking your setup…
      </p>
    )
  }
  if (setup.isError || layout === undefined) {
    return (
      <Card size="sm" tone="bad" className="max-w-xl" role="alert">
        <CardHeader>
          <CardTitle level={2}>Setup couldn’t be read</CardTitle>
        </CardHeader>
        <CardContent>
          <p>{setup.error?.message ?? 'No answer from the backend.'}</p>
        </CardContent>
      </Card>
    )
  }
  return (
    <div className="flex flex-col gap-3">
      <p className="text-muted-foreground">
        {summary(setup.data)} Each box opens the page for that step.
      </p>
      {skip.isError && (
        <p role="alert" className="text-sm text-destructive">
          {skip.error.message}
        </p>
      )}
      <FlowDiagram
        layout={layout}
        onSkip={(key, skipped) => skip.mutate({ key, skipped })}
        pending={skip.isPending ? skip.variables.key : undefined}
      />
    </div>
  )
}

function summary(setup: Setup): string {
  const required = setup.steps.filter((step) => !step.optional)
  const done = required.filter((step) => step.state === 'done').length
  if (setup.complete) return 'Setup is complete: every required step is done or skipped.'
  return `${done} of ${required.length} required steps done.`
}

interface Box {
  x: number
  y: number
  width: number
  height: number
}

function sameBoxes(left: Map<string, Box>, right: Map<string, Box>): boolean {
  if (left.size !== right.size) return false
  for (const [key, box] of right) {
    const other = left.get(key)
    if (
      other === undefined ||
      other.x !== box.x ||
      other.y !== box.y ||
      other.width !== box.width ||
      other.height !== box.height
    )
      return false
  }
  return true
}

/** Each box's place relative to the diagram, measured after layout and on resize. */
function useBoxes(layout: Layout) {
  const container = useRef<HTMLOListElement>(null)
  const items = useRef(new Map<string, HTMLElement>())
  const [boxes, setBoxes] = useState<Map<string, Box>>(new Map())
  const [size, setSize] = useState({ width: 0, height: 0 })

  useLayoutEffect(() => {
    const root = container.current
    if (root === null) return
    const measure = () => {
      const origin = root.getBoundingClientRect()
      const next = new Map<string, Box>()
      items.current.forEach((element, key) => {
        const rect = element.getBoundingClientRect()
        next.set(key, {
          x: rect.left - origin.left,
          y: rect.top - origin.top,
          width: rect.width,
          height: rect.height,
        })
      })
      setBoxes((previous) => (sameBoxes(previous, next) ? previous : next))
      setSize((previous) =>
        previous.width === origin.width && previous.height === origin.height
          ? previous
          : { width: origin.width, height: origin.height },
      )
    }
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    observer.observe(root)
    items.current.forEach((element) => observer.observe(element))
    return () => observer.disconnect()
  }, [layout])

  const register = useCallback(
    (key: string) => (element: HTMLElement | null) => {
      if (element === null) items.current.delete(key)
      else items.current.set(key, element)
    },
    [],
  )
  return { container, register, boxes, size }
}

function FlowDiagram({
  layout,
  onSkip,
  pending,
}: {
  layout: Layout
  onSkip: (key: string, skipped: boolean) => void
  pending: string | undefined
}) {
  const { container, register, boxes, size } = useBoxes(layout)
  const done = new Set(
    layout.placed.filter((item) => item.step.state === 'done').map((item) => item.step.key),
  )
  const titles = new Map(layout.placed.map((item) => [item.step.key, item.step.title]))

  return (
    <div className="relative">
      <svg
        aria-hidden="true"
        data-testid="steps-arrows"
        className="pointer-events-none absolute inset-0 hidden overflow-visible md:block"
        width={size.width}
        height={size.height}
      >
        {layout.edges.map((edge) => {
          const from = boxes.get(edge.from)
          const to = boxes.get(edge.to)
          if (from === undefined || to === undefined) return null
          return (
            <Arrow
              fromAt={edge.fromAt}
              toAt={edge.toAt}
              key={`${edge.from}-${edge.to}`}
              from={from}
              to={to}
              lit={done.has(edge.from)}
              dashed={edge.any}
            />
          )
        })}
      </svg>
      <ol
        ref={container}
        aria-label="Setup steps"
        className="relative grid grid-cols-1 gap-3 md:grid-cols-[repeat(var(--columns),minmax(0,1fr))] md:gap-x-16"
        style={{ '--columns': layout.columns } as CSSProperties}
      >
        {layout.placed.map(({ step, column, row }) => (
          <li
            key={step.key}
            ref={register(step.key)}
            data-step={step.key}
            className="md:[grid-column:var(--column)] md:[grid-row:var(--row)]"
            style={{ '--column': column + 1, '--row': row + 1 } as CSSProperties}
          >
            <StepBox
              step={step}
              needs={needsText(step, titles)}
              onSkip={onSkip}
              pending={pending === step.key}
            />
          </li>
        ))}
      </ol>
    </div>
  )
}

/** A curve from the right edge of `from` to the left edge of `to`, with an arrowhead. */
function Arrow({
  from,
  to,
  fromAt,
  toAt,
  lit,
  dashed,
}: {
  from: Box
  to: Box
  fromAt: number
  toAt: number
  lit: boolean
  dashed: boolean
}) {
  const x1 = from.x + from.width
  const y1 = from.y + from.height * fromAt
  const x2 = to.x - 2
  const y2 = to.y + to.height * toAt
  const bend = Math.max((x2 - x1) / 2, 12)
  return (
    <g className={lit ? 'text-ok' : 'text-muted-foreground/60'}>
      <path
        d={`M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2 - 6} ${y2}`}
        fill="none"
        stroke="currentColor"
        strokeWidth={lit ? 2 : 1.5}
        strokeDasharray={dashed ? '5 4' : undefined}
      />
      <path d={`M ${x2} ${y2} l -7 -4 v 8 z`} fill="currentColor" />
    </g>
  )
}

const LOOK_CLASSES: Record<BoxLook, string> = {
  done: '',
  next: '',
  // Not ready: no glass and a dashed rim, so the pane recedes; the text keeps the
  // muted ink, which stays at 4.5:1 on the page.
  waiting:
    'bg-transparent shadow-none outline-1 outline-border outline-dashed backdrop-blur-none backdrop-filter-none',
  skipped: 'outline-2 outline-muted-foreground/40 outline-dotted',
  open: '',
}

const BADGE_CLASSES: Record<BoxLook, string> = {
  done: 'bg-ok-soft text-ok-ink',
  next: 'bg-accent-soft text-accent-ink',
  waiting: 'bg-muted text-muted-foreground',
  skipped: 'bg-muted text-muted-foreground',
  open: 'bg-muted text-foreground',
}

function StepBox({
  step,
  needs,
  onSkip,
  pending,
}: {
  step: SetupStep
  needs: string | null
  onSkip: (key: string, skipped: boolean) => void
  pending: boolean
}) {
  const look = boxLook(step)
  const muted = look === 'waiting' || look === 'skipped'
  return (
    <Card
      size="sm"
      tone={BOX_TONE[look]}
      data-look={look}
      className={cn('h-full', LOOK_CLASSES[look])}
    >
      <CardHeader className="gap-1.5">
        <div className="flex flex-wrap items-center gap-2">
          <Badge className={BADGE_CLASSES[look]}>
            {look === 'next' && (
              <span
                aria-hidden="true"
                className="size-1.5 rounded-full bg-accent-ink motion-safe:animate-pulse"
              />
            )}
            {stateLabel(step)}
          </Badge>
          {step.optional && <span className="text-xs text-muted-foreground">Optional</span>}
        </div>
        <CardTitle level={3} className={cn(muted && 'text-muted-foreground')}>
          {step.title}
        </CardTitle>
        <p className="text-sm text-muted-foreground">{step.reason}</p>
        {needs !== null && <p className="text-xs text-muted-foreground md:sr-only">{needs}</p>}
      </CardHeader>
      <CardContent className="mt-auto flex flex-wrap items-center gap-2">
        <TargetLink target={step.target} label={TARGET_LABELS[step.target] ?? 'Open'} />
        {step.state !== 'done' && (
          <Button
            size="sm"
            variant="ghost"
            disabled={pending}
            onClick={() => onSkip(step.key, !step.skipped)}
            aria-label={`${step.skipped ? 'Un-skip' : 'Skip'} ${step.title}`}
          >
            {step.skipped ? 'Un-skip' : 'Skip'}
          </Button>
        )}
      </CardContent>
    </Card>
  )
}

/**
 * The page a step's work happens on. `render` clones the `Link` itself, as
 * `setup-step-card.tsx` explains; the lists route carries its own search schema.
 */
function TargetLink({ target, label }: { target: string; label: string }) {
  const props = { size: 'sm', variant: 'outline' } as const
  switch (target) {
    case '/lists':
      return (
        <Button {...props} render={<Link to="/lists" search={crmSearch(undefined)} />}>
          {label}
        </Button>
      )
    case '/imports':
      return (
        <Button {...props} render={<Link to="/imports" />}>
          {label}
        </Button>
      )
    case '/settings':
    case '/linkedin':
    case '/triage':
    case '/campaigns':
      return (
        <Button {...props} render={<Link to={target} />}>
          {label}
        </Button>
      )
    default:
      return null
  }
}
