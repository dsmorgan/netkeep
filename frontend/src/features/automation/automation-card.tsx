import { Radio } from '@base-ui/react/radio'
import { RadioGroup } from '@base-ui/react/radio-group'
import { useId } from 'react'

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { toneClasses } from '@/components/ui/tone'
import { cn } from '@/lib/utils'

import { DEPTH_LABEL, type Depth, type Position } from './api'
import { AutomationLadder } from './automation-switch'
import {
  useGmailAutomation,
  useLinkedInAutomation,
  type GmailControl,
  type LinkedInControl,
} from './use-automation'

const LINKEDIN_LINES: Record<Position, string> = {
  off: 'Leaves LinkedIn alone',
  manual: 'Runs when you click Start',
  automatic: 'Runs on its own, within active hours',
}

const DEPTH_LINE: Record<Depth, string> = {
  quick:
    'New connections and the LinkedIn inbox. You run a full sync yourself when you want removed connections noticed.',
  full: 'Adds the weekly full sync and profile refreshes for stale contacts. More time on LinkedIn each week.',
}

function gmailLines(replyEvery: string): Record<Position, string> {
  return {
    off: 'Leaves Gmail alone',
    manual: 'Writes drafts; you send them',
    automatic: `Sends within sending hours; checks replies ${replyEvery}`,
  }
}

/** The server's reasons come without a full stop; the page shows them as sentences. */
function sentence(text: string): string {
  return /[.!?]$/.test(text) ? text : `${text}.`
}

/** A failure beside a switch: a move refused, or the switch that couldn't load. */
export function SwitchError({ text }: { text: string | null }) {
  if (text === null) return null
  return (
    <p role="alert" className="rounded-lg bg-bad-soft px-3 py-2 text-sm text-destructive">
      {text}
    </p>
  )
}

/**
 * Settings › Automation (#829, mockup C): the same three positions for each service,
 * and How thorough under LinkedIn when it's on Automatic. Moving left takes effect at
 * once; moving right that does more asks first.
 */
export function AutomationCard() {
  const linkedin = useLinkedInAutomation()
  const gmail = useGmailAutomation()
  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle level={2}>Automation</CardTitle>
        <CardDescription>
          The same three positions for each service. Moving left takes effect at once.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-6">
        <LinkedInSection control={linkedin} />
        <GmailSection control={gmail} />
      </CardContent>
      {linkedin.dialog}
      {gmail.dialog}
    </Card>
  )
}

function LinkedInSection({ control }: { control: LinkedInControl }) {
  const { state } = control
  return (
    <section aria-labelledby="automation-linkedin" className="flex flex-col gap-3.5">
      <h3 id="automation-linkedin" className="font-heading text-base font-bold">
        LinkedIn
      </h3>
      <SwitchError text={control.loadError} />
      {state === undefined ? (
        control.loadError === null && <p role="status">Loading…</p>
      ) : (
        <>
          <AutomationLadder
            label="LinkedIn automation"
            value={state.position}
            onChoose={(position) => control.choose({ position })}
            lines={LINKEDIN_LINES}
            busy={control.pending}
          />
          {state.position === 'automatic' && (
            <DepthChoice
              value={state.depth}
              onChoose={(depth) => control.choose({ depth })}
              busy={control.pending}
            />
          )}
          <SwitchError text={control.error} />
        </>
      )}
    </section>
  )
}

function DepthChoice({
  value,
  onChoose,
  busy,
}: {
  value: Depth
  onChoose: (depth: Depth) => void
  busy: boolean
}) {
  const id = useId()
  return (
    <div className={cn(toneClasses('ok'), 'nk-flat flex flex-col gap-2 rounded-lg px-4 py-3.5')}>
      <p id={id} className="text-sm font-extrabold text-ok-ink">
        How thorough
      </p>
      <RadioGroup
        aria-labelledby={id}
        aria-busy={busy || undefined}
        value={value}
        readOnly={busy}
        onValueChange={(next) => onChoose(next as Depth)}
        className="flex flex-col gap-1"
      >
        {(['quick', 'full'] as const).map((depth) => (
          <Radio.Root
            key={depth}
            value={depth}
            aria-labelledby={`${id}-${depth}`}
            aria-describedby={`${id}-${depth}-line`}
            className="group flex min-h-11 cursor-pointer items-start gap-2.5 rounded-md py-1 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <span
              aria-hidden="true"
              className="mt-0.5 inline-flex size-4.5 shrink-0 items-center justify-center rounded-full border-2 border-muted-foreground group-data-checked:border-ok-ink"
            >
              <span className="size-2 rounded-full group-data-checked:bg-ok-ink" />
            </span>
            <span className="flex flex-col gap-0.5">
              <span id={`${id}-${depth}`} className="text-sm font-bold">
                {DEPTH_LABEL[depth]}
              </span>
              <span id={`${id}-${depth}-line`} className="text-xs text-muted-foreground">
                {DEPTH_LINE[depth]}
              </span>
            </span>
          </Radio.Root>
        ))}
      </RadioGroup>
    </div>
  )
}

function GmailSection({ control }: { control: GmailControl }) {
  const { state } = control
  const reasonId = useId()
  return (
    <section aria-labelledby="automation-gmail" className="flex flex-col gap-3.5">
      <h3 id="automation-gmail" className="font-heading text-base font-bold">
        Gmail
      </h3>
      <SwitchError text={control.loadError} />
      {state === undefined ? (
        control.loadError === null && <p role="status">Loading…</p>
      ) : (
        <>
          <AutomationLadder
            label="Gmail automation"
            value={state.position}
            onChoose={control.choose}
            lines={gmailLines(control.replyEvery)}
            disabled={{ automatic: control.blockedReason !== null }}
            disabledReasonId={reasonId}
            busy={control.pending}
          />
          {control.blockedReason !== null && (
            <p id={reasonId} className="text-sm text-muted-foreground">
              {sentence(control.blockedReason)}
            </p>
          )}
          <SwitchError text={control.error} />
        </>
      )}
    </section>
  )
}
