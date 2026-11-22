import { useId, type ReactNode } from 'react'

import { StaleError, SwitchError } from './automation-card'
import { FullSyncCheckbox } from './automation-compact'
import { AutomationSegments } from './automation-switch'
import { gmailLines, LINKEDIN_LINES, sentence } from './lines'
import { useGmailAutomation, useLinkedInAutomation } from './use-automation'

interface BarProps {
  /** The bar's heading, and the switch's accessible name. */
  label: string
  /** The anchor a link can name, such as `/linkedin#schedule`. */
  id?: string
  loadError: string | null
  /** The switch, once the server answered; undefined shows Loading. */
  children: ReactNode | undefined
  /** Lines under the row: why a stop is unavailable, a failed refresh, a refused move. */
  below?: ReactNode
}

/**
 * A thin, full-width bar for a service's automation switch (#835), first under the top
 * bar on the LinkedIn and Gmail pages, above the shell's banners. One row on a wide
 * screen: the label, the segments, any extra choice and the current position's line;
 * the row wraps on a narrow one.
 */
function AutomationBar({ label, id, loadError, children, below }: BarProps) {
  const headingId = useId()
  return (
    <section
      id={id}
      aria-labelledby={headingId}
      data-testid="automation-bar"
      className="nk-glass mb-4 flex max-w-6xl scroll-mt-4 flex-col gap-2 px-4 py-2.5"
    >
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <h2 id={headingId} className="font-heading text-sm font-bold">
          {label}
        </h2>
        {children === undefined
          ? loadError === null && (
              <p role="status" className="text-sm text-muted-foreground">
                Loading…
              </p>
            )
          : children}
      </div>
      <SwitchError text={loadError} />
      {below}
    </section>
  )
}

/** The position's line, beside the switch. */
function PositionLine({ text }: { text: string }) {
  return (
    <p
      data-testid="position-line"
      className="min-w-0 flex-1 basis-56 text-sm text-muted-foreground"
    >
      {text}
    </p>
  )
}

/**
 * The LinkedIn page's bar: Off, Manual and Automatic, the weekly full sync checkbox on
 * Automatic, and what the position does. The same control as the pill's popover and
 * Settings › Automation: moving to Automatic, or adding the full sync, asks first.
 * Its anchor is `schedule`, where the dashboard and the status rows link.
 */
export function LinkedInAutomationBar() {
  const control = useLinkedInAutomation()
  const { state } = control
  return (
    <>
      <AutomationBar
        label="LinkedIn automation"
        id="schedule"
        loadError={control.loadError}
        below={
          <>
            <StaleError text={control.staleError} />
            <SwitchError text={control.error} />
          </>
        }
      >
        {state !== undefined && (
          <>
            <div className="w-full sm:w-80">
              <AutomationSegments
                label="LinkedIn automation"
                value={state.position}
                onChoose={(position) => control.choose({ position })}
                busy={control.pending}
              />
            </div>
            {state.position === 'automatic' && <FullSyncCheckbox control={control} />}
            <PositionLine text={sentence(LINKEDIN_LINES[state.position])} />
          </>
        )}
      </AutomationBar>
      {control.dialog}
    </>
  )
}

/**
 * The Gmail page's bar: Off, Manual (drafts only) and Automatic, and what the position
 * does. While the server says Automatic isn't ready (#510, or a mailbox to sign in
 * again), that stop is disabled and the bar says why.
 */
export function GmailAutomationBar() {
  const control = useGmailAutomation()
  const { state } = control
  const reasonId = useId()
  return (
    <>
      <AutomationBar
        label="Gmail automation"
        loadError={control.loadError}
        below={
          <>
            {control.blockedReason !== null && (
              <p id={reasonId} className="text-sm text-muted-foreground">
                Automatic isn’t available: {sentence(control.blockedReason)}
              </p>
            )}
            <StaleError text={control.staleError} />
            <SwitchError text={control.error} />
          </>
        }
      >
        {state !== undefined && (
          <>
            <div className="w-full sm:w-80">
              <AutomationSegments
                label="Gmail automation"
                value={state.position}
                onChoose={control.choose}
                disabled={{ automatic: control.blockedReason !== null }}
                disabledReasonId={reasonId}
                busy={control.pending}
              />
            </div>
            <PositionLine text={sentence(gmailLines(control.replyEvery)[state.position])} />
          </>
        )}
      </AutomationBar>
      {control.dialog}
    </>
  )
}
