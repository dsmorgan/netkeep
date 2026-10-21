import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { useCallback, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { toneClasses } from '@/components/ui/tone'
import { crmSearch } from '@/features/crm/crm-tabs'
import { useServerEvent } from '@/features/events/use-server-event'
import { cn } from '@/lib/utils'

import { setSkipped, setupKeys, setupQuery, type Setup, type SetupStep } from './setup-api'
import {
  BOX_WIDTH,
  STEP_ANCHORS,
  actionLabel,
  boxLook,
  fitsMap,
  placeMap,
  routeWires,
  skipAction,
  stateLabel,
  wireLook,
  type BoxLook,
  type Measured,
  type Placement,
  type Rect,
  type Size,
  type WireLook,
} from './setup-map-layout'

const HEADING_ID = 'setup-map-heading'

/**
 * The setup map (#740): one small box per step from `GET /setup`, joined by
 * right-angled connectors. Every state comes from the server; a box links to the page
 * that does its step and never starts anything there.
 *
 * The boxes are an ordered list in number order, so a screen reader hears each step
 * with its state; the connectors are an SVG hidden from assistive tech. On a narrow
 * screen the map scrolls sideways inside its pane. Once setup is complete, the map
 * folds to one line that can show it again.
 */
export function SetupMap() {
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
  const [shown, setShown] = useState(false)

  let body
  if (setup.isPending) {
    body = (
      <p role="status" className="text-muted-foreground">
        Checking your setup…
      </p>
    )
  } else if (setup.isError) {
    body = (
      <Card size="sm" tone="bad" className="max-w-xl" role="alert">
        <CardHeader>
          <CardTitle level={3}>Setup couldn’t be read</CardTitle>
        </CardHeader>
        <CardContent>
          <p>{setup.error.message}</p>
        </CardContent>
      </Card>
    )
  }

  const data = setup.data
  const folded = data?.complete === true && !shown
  return (
    <section
      aria-labelledby={HEADING_ID}
      data-region="setup"
      className="nk-glass flex flex-col gap-3 p-(--space-card-sm)"
    >
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <h2 id={HEADING_ID} className="font-heading text-card-title font-bold tracking-heading">
          {data?.complete === true ? 'Setup is complete' : 'Setup'}
        </h2>
        {data !== undefined && !folded && <Progress setup={data} />}
        {data?.complete === true && (
          <Button size="sm" variant="outline" onClick={() => setShown(!shown)}>
            {shown ? 'Hide the steps' : 'Show the steps'}
          </Button>
        )}
      </div>
      {body}
      {data !== undefined && !folded && (
        <>
          {skip.isError && (
            <p role="alert" className="text-sm text-destructive">
              {skip.error.message}
            </p>
          )}
          <StepsMap
            setup={data}
            onSkip={(key, skipped) => skip.mutate({ key, skipped })}
            pending={skip.isPending ? skip.variables.key : undefined}
          />
          <Legend />
        </>
      )}
    </section>
  )
}

/** "6 of 10 required steps done or skipped · next: 6 First inbox read", and a bar. */
function Progress({ setup }: { setup: Setup }) {
  const required = setup.steps.filter((step) => !step.optional)
  const reached = required.filter((step) => step.state === 'done' || step.state === 'skipped')
  const next = setup.steps
    .filter((step) => step.next)
    .sort((left, right) => left.number - right.number)[0]
  const percent = required.length === 0 ? 100 : (reached.length / required.length) * 100
  return (
    <div className="flex items-center gap-3 text-sm">
      <p data-testid="setup-progress">
        <span className="font-semibold tabular-nums">
          {reached.length} of {required.length}
        </span>{' '}
        required steps done or skipped
        {next !== undefined && (
          <>
            {' · next: '}
            <span className="font-semibold">
              {next.number} {next.title}
            </span>
          </>
        )}
      </p>
      <span aria-hidden="true" className="h-1.5 w-32 overflow-hidden rounded-full bg-muted">
        <span className="block h-full rounded-full bg-primary" style={{ width: `${percent}%` }} />
      </span>
    </div>
  )
}

function Legend() {
  return (
    <p className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
      <span className="flex items-center gap-1.5">
        <span aria-hidden="true" className="size-2 rounded-full bg-ok" /> Done
      </span>
      <span className="flex items-center gap-1.5">
        <span
          aria-hidden="true"
          className="size-2 rounded-full bg-primary motion-safe:animate-pulse"
        />{' '}
        Next
      </span>
      <span className="flex items-center gap-1.5">
        <span aria-hidden="true" className="size-2 rounded-full bg-muted-foreground" /> To do or
        waiting
      </span>
      <span>Dashed boxes are optional. Do the Gmail step, the LinkedIn steps, or both.</span>
    </p>
  )
}

type Sizes = Partial<Record<Measured, Size>>

function sameSizes(left: Sizes, right: Sizes): boolean {
  const keys = Object.keys(right) as Measured[]
  if (Object.keys(left).length !== keys.length) return false
  return keys.every(
    (key) => left[key]?.width === right[key]?.width && left[key]?.height === right[key]?.height,
  )
}

/**
 * Each node's rendered size, measured after it renders, again when a node resizes
 * (its text wraps, the fonts load) and when the window does. A box's width is fixed
 * and its place is computed, so its size is all the layout needs to know.
 */
function useSizes(content: unknown) {
  const nodes = useRef(new Map<Measured, HTMLElement>())
  const refs = useRef(new Map<Measured, (element: HTMLElement | null) => void>())
  const [sizes, setSizes] = useState<Sizes>({})

  useLayoutEffect(() => {
    const measure = () => {
      const next: Sizes = {}
      nodes.current.forEach((element, key) => {
        next[key] = { width: element.offsetWidth, height: element.offsetHeight }
      })
      setSizes((previous) => (sameSizes(previous, next) ? previous : next))
    }
    measure()
    let live = true
    // `document.fonts` is missing in jsdom.
    void (document.fonts as FontFaceSet | undefined)?.ready.then(() => {
      if (live) measure()
    })
    window.addEventListener('resize', measure)
    const observer = typeof ResizeObserver === 'undefined' ? undefined : new ResizeObserver(measure)
    nodes.current.forEach((element) => observer?.observe(element))
    return () => {
      live = false
      window.removeEventListener('resize', measure)
      observer?.disconnect()
    }
  }, [content])

  /** One stable ref callback per node, so a render never detaches and re-attaches it. */
  const register = useCallback((key: Measured) => {
    let ref = refs.current.get(key)
    if (ref === undefined) {
      ref = (element) => {
        if (element === null) nodes.current.delete(key)
        else nodes.current.set(key, element)
      }
      refs.current.set(key, ref)
    }
    return ref
  }, [])

  return { sizes, register }
}

const place = (rect: Rect) => ({ left: rect.left, top: rect.top })

function StepsMap({
  setup,
  onSkip,
  pending,
}: {
  setup: Setup
  onSkip: (key: string, skipped: boolean) => void
  pending: string | undefined
}) {
  const ordered = useMemo(
    () => [...setup.steps].sort((left, right) => left.number - right.number),
    [setup.steps],
  )
  const { sizes, register } = useSizes(setup)
  const placement = useMemo(() => placeMap(sizes), [sizes])

  // Steps the map has no place for (a server newer than this page): a plain list.
  if (!fitsMap(ordered)) {
    return (
      <ol aria-label="Setup steps" className="flex flex-wrap gap-3">
        {ordered.map((step) => (
          <li key={step.key} data-step={step.key} style={{ width: BOX_WIDTH }}>
            <StepBox step={step} onSkip={onSkip} pending={pending === step.key} />
          </li>
        ))}
      </ol>
    )
  }

  return (
    <div className="-mx-1 overflow-x-auto p-1" data-testid="setup-map-scroll">
      <div
        className="relative mx-auto"
        style={{ width: placement.width, height: placement.height }}
      >
        <Groups placement={placement} measureOr={register('or')} />
        <ol aria-label="Setup steps">
          {ordered.map((step) => (
            <li
              key={step.key}
              ref={register(step.key as Measured)}
              data-step={step.key}
              className="absolute"
              style={{
                ...place(placement.nodes[step.key as Exclude<Measured, 'or'>]),
                width: BOX_WIDTH,
              }}
            >
              <StepBox step={step} onSkip={onSkip} pending={pending === step.key} />
            </li>
          ))}
        </ol>
        <EndPill label="Start" reached rect={placement.nodes.start} measure={register('start')} />
        <EndPill
          label="Ready"
          reached={setup.complete}
          rect={placement.nodes.ready}
          measure={register('ready')}
        />
        <Wires setup={setup} placement={placement} />
      </div>
    </div>
  )
}

/** The Gmail and LinkedIn groups' dashed outlines, and "one or both" between them. */
function Groups({
  placement,
  measureOr,
}: {
  placement: Placement
  measureOr: (element: HTMLElement | null) => void
}) {
  const group = (label: string, rect: Rect) => (
    <div
      aria-hidden="true"
      data-group={label}
      className="absolute rounded-2xl border-[1.5px] border-dashed border-muted-foreground/40"
      style={{ ...place(rect), width: rect.width, height: rect.height }}
    >
      <span className="absolute -top-2.5 left-3 rounded-sm bg-card px-1.5 text-[10.5px] font-extrabold tracking-[0.08em] text-muted-foreground uppercase">
        {label}
      </span>
    </div>
  )
  return (
    <>
      {group('Gmail', placement.groups.gmail)}
      {group('LinkedIn', placement.groups.linkedin)}
      <span
        ref={measureOr}
        aria-hidden="true"
        className="absolute rounded-full bg-muted px-2 py-px text-[11px] font-bold whitespace-nowrap text-muted-foreground"
        style={place(placement.or)}
      >
        one or both
      </span>
    </>
  )
}

function EndPill({
  label,
  reached,
  rect,
  measure,
}: {
  label: string
  reached: boolean
  rect: Rect
  measure: (element: HTMLElement | null) => void
}) {
  return (
    <span
      ref={measure}
      aria-hidden="true"
      data-end={label}
      className={cn(
        'absolute flex w-21 items-center justify-center gap-1.5 rounded-full border-[1.5px] px-3 py-1.5 text-[12.5px] font-bold',
        reached
          ? 'border-ok/50 bg-ok-soft text-ok-ink'
          : 'border-dashed border-border text-muted-foreground',
      )}
      style={place(rect)}
    >
      <span className={cn('size-2 rounded-full', reached ? 'bg-ok' : 'bg-muted-foreground')} />
      {label}
    </span>
  )
}

const WIRE_CLASSES: Record<WireLook, string> = {
  done: 'stroke-ok/75',
  next: 'nk-wire-next stroke-primary',
  alt: 'stroke-muted-foreground/55 [stroke-dasharray:2_5]',
  base: 'stroke-muted-foreground/55 [stroke-dasharray:4_4]',
}

const HEAD_CLASSES: Record<'done' | 'next' | 'base', string> = {
  done: 'fill-ok/85',
  next: 'fill-primary',
  base: 'fill-muted-foreground/80',
}

/** The connectors, drawn over the boxes from their placed rects. */
function Wires({ setup, placement }: { setup: Setup; placement: Placement }) {
  const id = useId()
  const steps = useMemo(() => new Map(setup.steps.map((step) => [step.key, step])), [setup.steps])
  const wires = routeWires(placement)
  return (
    <svg
      aria-hidden="true"
      data-testid="setup-wires"
      className="pointer-events-none absolute inset-0 overflow-visible"
      width={placement.width}
      height={placement.height}
    >
      <defs>
        {(['done', 'next', 'base'] as const).map((head) => (
          <marker
            key={head}
            id={`${id}-${head}`}
            viewBox="0 0 10 10"
            refX="10"
            refY="5"
            markerWidth="9"
            markerHeight="9"
            markerUnits="userSpaceOnUse"
            orient="auto-start-reverse"
          >
            <path d="M0,0 L10,5 L0,10 z" className={HEAD_CLASSES[head]} />
          </marker>
        ))}
      </defs>
      {wires.map((wire) => {
        const look = wireLook(wire, steps, setup.complete)
        const head = look === 'alt' ? 'base' : look
        return (
          <path
            key={`${wire.from}-${wire.to}`}
            data-wire={`${wire.from}-${wire.to}`}
            data-look={look}
            d={wire.d}
            fill="none"
            strokeWidth={look === 'next' ? 2 : 1.6}
            strokeLinejoin="round"
            className={WIRE_CLASSES[look]}
            markerEnd={`url(#${id}-${head})`}
          />
        )
      })}
    </svg>
  )
}

/** Each look's pane: done a filled `ok` pane, next the accent tint, waiting dimmed. */
const BOX_CLASSES: Record<BoxLook, string> = {
  done: cn('nk-glass', toneClasses('ok')),
  next: cn('nk-glass', toneClasses('accent')),
  todo: 'nk-glass',
  // No glass and no shadow, so the pane recedes; the text keeps the muted ink, which
  // stays at 4.5:1 on the map's pane. The rim is a ring, like the glass panes', not a
  // border, so a box is the same height in every state and row 2's connectors run level.
  waiting: 'bg-transparent ring-1 ring-border',
  skipped: 'bg-transparent ring-1 ring-border',
}

const NUMBER_CLASSES: Record<BoxLook, string> = {
  // `ok-ink`, not `ok`: card text on `ok` is 4.41:1 in light; `theme.test.ts` holds this pair.
  done: 'bg-ok-ink text-card',
  next: 'bg-primary text-primary-foreground',
  todo: 'bg-muted text-muted-foreground',
  waiting: 'bg-muted text-muted-foreground',
  skipped: 'bg-muted text-muted-foreground',
}

const BADGE_CLASSES: Record<BoxLook, string> = {
  done: 'bg-ok-soft text-ok-ink',
  next: 'bg-accent-soft text-accent-ink',
  todo: 'bg-muted text-muted-foreground',
  waiting: 'bg-muted text-muted-foreground',
  skipped: 'bg-muted text-muted-foreground',
}

/**
 * The title as the box shows it: "Run at login (optional)" loses its "(optional)", which
 * a 140 px box would cut off and the dashed border already shows. A screen reader still
 * hears it.
 */
function shownTitle(step: SetupStep): string {
  return step.optional ? step.title.replace(/\s*\(optional\)$/i, '') : step.title
}

function StepBox({
  step,
  onSkip,
  pending,
}: {
  step: SetupStep
  onSkip: (key: string, skipped: boolean) => void
  pending: boolean
}) {
  const look = boxLook(step)
  const skip = skipAction(step)
  return (
    <div
      data-look={look}
      data-tone={look === 'done' ? 'ok' : look === 'next' ? 'accent' : undefined}
      data-optional={step.optional || undefined}
      className={cn(
        'flex flex-col gap-0.5 rounded-[calc(var(--radius)*0.8)] px-2.5 pt-2 pb-1.5',
        BOX_CLASSES[look],
        step.optional && 'outline-1 -outline-offset-1 outline-muted-foreground/60 outline-dashed',
      )}
    >
      <div className="flex items-center justify-between gap-1.5">
        <span
          className={cn(
            'rounded-[5px] px-1.5 py-0.5 font-mono text-[11px] leading-none font-extrabold',
            NUMBER_CLASSES[look],
          )}
        >
          <span className="sr-only">Step </span>
          {step.number}
        </span>
        <Badge
          className={cn(
            'h-4 rounded-sm px-1.5 text-[9.5px] font-extrabold tracking-wide uppercase',
            BADGE_CLASSES[look],
          )}
        >
          {stateLabel(step)}
        </Badge>
      </div>
      <h3
        title={step.title}
        className={cn(
          'truncate text-[12.5px] leading-tight font-bold',
          (look === 'waiting' || look === 'skipped') && 'text-muted-foreground',
          look === 'skipped' && 'line-through',
        )}
      >
        {shownTitle(step)}
        {shownTitle(step) !== step.title && (
          <>
            {' '}
            <span className="sr-only">(optional)</span>
          </>
        )}
      </h3>
      <p title={step.reason} className="truncate text-[11px] text-muted-foreground">
        {step.reason}
      </p>
      <div className="mt-0.5 flex items-center gap-0.5">
        <TargetLink step={step} primary={look === 'next'} />
        {skip !== null && (
          <Button
            size="xs"
            variant="ghost"
            className="text-muted-foreground"
            disabled={pending}
            onClick={() => onSkip(step.key, skip === 'skip')}
            aria-label={`${skip === 'skip' ? 'Skip' : 'Undo skip of'} ${step.title}`}
          >
            {skip === 'skip' ? 'Skip' : 'Undo skip'}
          </Button>
        )}
      </div>
    </div>
  )
}

/**
 * The page a step's work happens on, scrolled to its section. `render` clones the
 * `Link` itself; the lists route carries its own search schema.
 */
function TargetLink({ step, primary }: { step: SetupStep; primary: boolean }) {
  const label = actionLabel(step)
  const props = {
    size: 'xs',
    variant: primary ? 'default' : 'outline',
    'aria-label': `${label} ${step.title}`,
  } as const
  const hash = STEP_ANCHORS[step.key]
  switch (step.target) {
    case '/lists':
      return (
        <Button {...props} render={<Link to="/lists" search={crmSearch(undefined)} />}>
          {label}
        </Button>
      )
    case '/imports':
    case '/settings':
    case '/linkedin':
    case '/triage':
    case '/campaigns':
      return (
        <Button {...props} render={<Link to={step.target} hash={hash} />}>
          {label}
        </Button>
      )
    default:
      return null
  }
}
