import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { useCallback, useId, useMemo, useState } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { toneClasses } from '@/components/ui/tone'
import { crmSearch } from '@/features/crm/crm-tabs'
import { useServerEvent } from '@/features/events/use-server-event'
import { cn } from '@/lib/utils'

import { setSkipped, setupKeys, setupQuery, type Setup, type SetupStep } from './setup-api'
import {
  CONNECT_SUBTITLE,
  STEP_ANCHORS,
  STEP_EXPLANATIONS,
  actionLabel,
  currentStep,
  groupSteps,
  groupSummary,
  markLook,
  settledCount,
  shownTitle,
  skipAction,
  startsFolded,
  stateLabel,
  type MarkLook,
  type SetupGroup,
} from './setup-steps'

const HEADING_ID = 'setup-heading'

/**
 * The setup checklist (#747): the steps from `GET /setup` as rows in four groups, each
 * group folding under its heading button. One row is open at a time, the step to do
 * now by default, with a sentence on the step and its buttons. Every state comes from
 * the server; a row's link opens the page that does its step and never starts anything
 * there.
 *
 * The rows run in number order, so a screen reader hears each step with its state.
 * Once setup is complete, the pane folds to one line that can show the steps again.
 */
export function SetupChecklist() {
  const queryClient = useQueryClient()
  const setup = useQuery(setupQuery)
  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: setupKeys.all })
  }, [queryClient])
  // A sync or an inbox read finishing can complete a step.
  useServerEvent('run.finished', refresh)
  useServerEvent('mailbox.status', refresh)
  // The open row: undefined is the default (the step to do now), null none.
  const [open, setOpen] = useState<string | null | undefined>(undefined)
  const skip = useMutation({
    mutationFn: setSkipped,
    onSuccess: (data) => {
      queryClient.setQueryData(setupQuery.queryKey, data)
      setOpen(undefined)
    },
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
      className="nk-glass @container flex flex-col gap-3 p-(--space-card-sm)"
    >
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <div className="flex flex-col">
          <span className="nk-eyebrow">Setup</span>
          <h2 id={HEADING_ID} className="font-heading text-card-title font-bold tracking-heading">
            {data?.complete === true ? 'Setup is complete' : 'Set up netkeep'}
          </h2>
        </div>
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
          <Groups
            setup={data}
            open={open === undefined ? currentStep(data.steps)?.key : (open ?? undefined)}
            onOpen={(key) => setOpen(key)}
            onSkip={(key, skipped) => skip.mutate({ key, skipped })}
            pending={skip.isPending ? skip.variables.key : undefined}
          />
        </>
      )}
    </section>
  )
}

/** "6 of 11 done · next: 6. First inbox read", and a bar. Every step counts. */
function Progress({ setup }: { setup: Setup }) {
  const reached = settledCount(setup.steps)
  const total = setup.steps.length
  const next = setup.complete ? undefined : currentStep(setup.steps)
  const percent = total === 0 ? 100 : (reached / total) * 100
  return (
    <div className="flex min-w-55 flex-1 items-center justify-end gap-2.5 text-sm">
      <p data-testid="setup-progress">
        <span className="font-semibold tabular-nums">
          {reached} of {total}
        </span>{' '}
        done
        {next !== undefined && (
          <>
            {' · next: '}
            <span className="font-semibold">
              {next.number}. {next.title}
            </span>
          </>
        )}
      </p>
      <span
        aria-hidden="true"
        className="h-1.5 max-w-55 min-w-20 flex-1 overflow-hidden rounded-full bg-muted"
      >
        <span className="block h-full rounded-full bg-primary" style={{ width: `${percent}%` }} />
      </span>
    </div>
  )
}

interface RowProps {
  open: string | undefined
  onOpen: (key: string | null) => void
  onSkip: (key: string, skipped: boolean) => void
  pending: string | undefined
}

function Groups({ setup, ...rows }: { setup: Setup } & RowProps) {
  const groups = useMemo(() => groupSteps(setup.steps), [setup.steps])
  // A heading's own choice, once clicked; until then a settled group starts folded.
  const [folds, setFolds] = useState<Partial<Record<string, boolean>>>({})
  return (
    <div className="flex flex-col">
      {groups.map((group, index) => {
        const folded = folds[group.key] ?? startsFolded(group)
        return (
          <Group
            key={group.key}
            group={group}
            first={index === 0}
            folded={folded}
            onFold={() => setFolds({ ...folds, [group.key]: !folded })}
            {...rows}
          />
        )
      })}
    </div>
  )
}

function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      className={cn(
        'size-4 flex-none fill-none stroke-current stroke-2 transition-transform motion-reduce:transition-none [stroke-linecap:round] [stroke-linejoin:round]',
        open && 'rotate-90',
      )}
    >
      <path d="M9 6l6 6-6 6" />
    </svg>
  )
}

function Group({
  group,
  first,
  folded,
  onFold,
  ...rows
}: {
  group: SetupGroup
  first: boolean
  folded: boolean
  onFold: () => void
} & RowProps) {
  const panel = useId()
  const summary = groupSummary(group)
  return (
    <div data-group={group.key} className={cn(!first && 'mt-2.5 border-t border-border pt-2.5')}>
      <h3>
        <button
          type="button"
          aria-expanded={!folded}
          aria-controls={folded ? undefined : panel}
          onClick={onFold}
          className="flex w-full items-center gap-2.5 rounded-control px-0.5 py-1 text-left text-muted-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <Chevron open={!folded} />
          <span className="text-[13px] font-extrabold tracking-[0.07em] uppercase">
            {group.title}
          </span>
          <span
            data-testid="group-summary"
            className={cn('ml-auto text-[12.5px]', summary.ok && 'font-bold text-ok-ink')}
          >
            {summary.text}
          </span>
        </button>
      </h3>
      {!folded && (
        <div id={panel}>
          {group.tracked !== undefined ? (
            <>
              <p className="px-0.5 pt-0.5 text-[12.5px] text-muted-foreground">
                {CONNECT_SUBTITLE}
              </p>
              <div
                data-testid="setup-tracks"
                className="mt-1.5 grid grid-cols-1 items-start gap-2.5 @min-[700px]:grid-cols-[minmax(0,1fr)_minmax(0,1.35fr)]"
              >
                {group.tracked.map((track) => (
                  <div
                    key={track.title}
                    data-track={track.title}
                    className="rounded-[10px] border border-dashed border-muted-foreground/40 px-2 pt-2 pb-1"
                  >
                    <h4 className="px-1.5 pb-1 text-[11px] font-extrabold tracking-[0.08em] text-muted-foreground uppercase">
                      {track.title}
                    </h4>
                    <Rows steps={track.items} label={track.title} {...rows} />
                  </div>
                ))}
              </div>
            </>
          ) : (
            <Rows steps={group.items} label={group.title} {...rows} />
          )}
        </div>
      )}
    </div>
  )
}

function Rows({ steps, label, ...rows }: { steps: SetupStep[]; label: string } & RowProps) {
  return (
    <ol aria-label={`${label} steps`} className="flex flex-col">
      {steps.map((step) => (
        <Row key={step.key} step={step} {...rows} />
      ))}
    </ol>
  )
}

/** Each mark's look: a check for done, the number otherwise, struck through if skipped. */
const MARK_CLASSES: Record<MarkLook, string> = {
  // `ok-ink`, not `ok`: card text on `ok` is 4.41:1 in light; `theme.test.ts` holds this pair.
  done: 'border-transparent bg-ok-ink text-card',
  next: 'border-2 border-primary bg-card text-accent-ink',
  todo: 'border-muted-foreground/60 bg-card text-muted-foreground',
  waiting: 'border-dashed border-muted-foreground/60 bg-card text-muted-foreground',
  skipped: 'border-muted-foreground/60 bg-card text-muted-foreground line-through',
}

function Mark({ step, look }: { step: SetupStep; look: MarkLook }) {
  return (
    <span
      aria-hidden="true"
      data-mark={look}
      className={cn(
        'grid size-6.5 flex-none place-items-center rounded-full border-[1.5px] font-mono text-[11.5px] leading-none font-extrabold',
        MARK_CLASSES[look],
      )}
    >
      {look === 'done' ? (
        <svg
          viewBox="0 0 24 24"
          className="size-3.5 fill-none stroke-current stroke-3 [stroke-linecap:round] [stroke-linejoin:round]"
        >
          <path d="M5 12.5l4.5 4.5L19 7.5" />
        </svg>
      ) : (
        step.number
      )}
    </span>
  )
}

function Row({ step, open, onOpen, onSkip, pending }: { step: SetupStep } & RowProps) {
  const body = useId()
  const look = markLook(step)
  const isOpen = open === step.key
  const done = step.state === 'done'
  const skip = skipAction(step)
  const title = shownTitle(step)
  return (
    <li
      data-step={step.key}
      data-row-open={isOpen || undefined}
      data-tone={isOpen ? (done ? 'ok' : 'accent') : undefined}
      className={cn('rounded-[10px]', isOpen && cn(toneClasses(done ? 'ok' : 'accent'), 'nk-flat'))}
    >
      <button
        type="button"
        aria-expanded={isOpen}
        aria-controls={isOpen ? body : undefined}
        onClick={() => onOpen(isOpen ? null : step.key)}
        className={cn(
          'flex w-full items-center gap-2.5 rounded-[10px] px-1.5 py-2 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring',
          !(isOpen && done) && 'hover:bg-muted/60',
        )}
      >
        <Mark step={step} look={look} />
        <span
          className={cn(
            'min-w-0 flex-1 text-sm font-bold',
            (look === 'done' || look === 'skipped') && 'font-semibold text-muted-foreground',
            look === 'skipped' && 'line-through',
            // A done row open is a green pane; its title takes the tint's heading color.
            isOpen &&
              done &&
              'font-bold text-[color-mix(in_oklab,var(--tone)_45%,var(--foreground))]',
          )}
        >
          <span className="sr-only">Step {step.number}:</span> {title}
          {title !== step.title && (
            <>
              {' '}
              <span className="sr-only">(optional)</span>
            </>
          )}
          <span className="sr-only">, {stateLabel(step)}.</span>
          {step.optional && (
            <Badge className="ml-1.5 h-4 rounded-sm bg-muted px-1.5 align-middle text-[10.5px] font-extrabold tracking-wide text-muted-foreground uppercase">
              Optional
            </Badge>
          )}
        </span>
        <span
          title={step.reason}
          className="max-w-[46%] truncate text-[12.5px] text-muted-foreground"
        >
          {step.reason}
        </span>
        <Chevron open={isOpen} />
      </button>
      {isOpen && (
        <div id={body} className="flex flex-col gap-2.5 pr-3 pb-3 pl-12">
          {STEP_EXPLANATIONS[step.key] !== undefined && (
            <p className="max-w-[62ch] text-[13.5px]">{STEP_EXPLANATIONS[step.key]}</p>
          )}
          <div className="flex flex-wrap items-center gap-1.5">
            <TargetLink step={step} />
            {skip !== null && (
              <Button
                size="sm"
                variant="ghost"
                className="text-muted-foreground"
                disabled={pending === step.key}
                onClick={() => onSkip(step.key, skip === 'skip')}
                aria-label={`${skip === 'skip' ? 'Skip' : 'Undo skip of'} ${step.title}`}
              >
                {skip === 'skip' ? 'Skip' : 'Undo skip'}
              </Button>
            )}
          </div>
        </div>
      )}
    </li>
  )
}

/**
 * The page a step's work happens on, scrolled to its section. A waiting step's Start
 * stays, disabled; its reason says what it waits for. `render` clones the `Link`
 * itself; the lists route carries its own search schema.
 */
function TargetLink({ step }: { step: SetupStep }) {
  const label = actionLabel(step)
  const props = {
    size: 'sm',
    variant: step.state === 'todo' || step.state === 'in_progress' ? 'default' : 'outline',
    'aria-label': `${label} ${step.title}`,
  } as const
  if (step.state === 'blocked') {
    return (
      <Button {...props} disabled>
        {label}
      </Button>
    )
  }
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
