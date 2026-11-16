import { useId } from 'react'

import { Checkbox } from '@/components/ui/checkbox'

import { AutomationSegments } from './automation-switch'
import { SwitchError } from './automation-card'
import type { GmailControl, LinkedInControl } from './use-automation'

/**
 * The LinkedIn switch in its compact form (#829, mockup D), for the LinkedIn pill's
 * popover and the LinkedIn page: Off, Manual and Automatic as segments, and on
 * Automatic one checkbox for the weekly full sync. The dialog that asks first is the
 * caller's to render (`control.dialog`), outside any popover.
 */
export function LinkedInSwitchCompact({ control }: { control: LinkedInControl }) {
  const { state } = control
  if (state === undefined) return <SwitchError text={control.loadError} />
  return (
    <div className="flex flex-col gap-2">
      <AutomationSegments
        label="LinkedIn automation"
        value={state.position}
        onChoose={(position) => control.choose({ position })}
        busy={control.pending}
      />
      {state.position === 'automatic' && (
        <label className="flex cursor-pointer items-start gap-2 text-xs">
          <Checkbox
            className="mt-px"
            checked={state.depth === 'full'}
            disabled={control.pending}
            onCheckedChange={(checked) => control.choose({ depth: checked ? 'full' : 'quick' })}
          />
          <span>Also run the weekly full sync and profile refreshes</span>
        </label>
      )}
      <SwitchError text={control.error} />
    </div>
  )
}

/** The Gmail switch in its compact form, for the Gmail pill's popover. */
export function GmailSwitchCompact({ control }: { control: GmailControl }) {
  const { state } = control
  const reasonId = useId()
  if (state === undefined) return <SwitchError text={control.loadError} />
  return (
    <div className="flex flex-col gap-2">
      <AutomationSegments
        label="Gmail automation"
        value={state.position}
        onChoose={control.choose}
        disabled={{ automatic: control.blockedReason !== null }}
        disabledReasonId={reasonId}
        busy={control.pending}
      />
      {control.blockedReason !== null && (
        <p id={reasonId} className="text-xs text-muted-foreground">
          {control.blockedReason}
        </p>
      )}
      <SwitchError text={control.error} />
    </div>
  )
}

/** "Automation", the label over a compact switch in a popover. */
export function AutomationHeading() {
  return (
    <p aria-hidden="true" className="text-xs font-bold text-muted-foreground">
      Automation
    </p>
  )
}
