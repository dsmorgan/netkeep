import { useId } from 'react'

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

import { AutomationLadder } from './automation-switch'
import { DepthChoice } from './depth-choice'
import { gmailLines, LINKEDIN_LINES, sentence } from './lines'
import {
  useGmailAutomation,
  useLinkedInAutomation,
  type GmailControl,
  type LinkedInControl,
} from './use-automation'

/** A failed refresh under a switch that still shows its last answer. */
export function StaleError({ text }: { text: string | null }) {
  if (text === null) return null
  return (
    <p role="status" className="text-xs text-destructive">
      {text}
    </p>
  )
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
          <StaleError text={control.staleError} />
          <SwitchError text={control.error} />
        </>
      )}
    </section>
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
          <StaleError text={control.staleError} />
          <SwitchError text={control.error} />
        </>
      )}
    </section>
  )
}
