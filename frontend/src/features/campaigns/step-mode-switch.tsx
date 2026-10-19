/**
 * Switch a running campaign's LinkedIn step between Prefill and Auto-send (#536).
 *
 * Switching to Auto-send asks first and says what changes; switching to Prefill does
 * not ask. Either way the switch itself sends nothing: contacts already queued for the
 * step stay on Prefill, and only contacts who reach the step later are sent on their
 * own, through every limit auto-send has.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { Callout, ErrorNote } from '@/features/crm/controls'

import {
  campaignKeys,
  errorText,
  setStepMode,
  stepModeQuery,
  type Campaign,
  type Step,
} from './api'

/** What a switch to Auto-send changes, as the API's `confirm_text` says it. */
export const AUTO_SEND_CONFIRM =
  "netkeep sends this step's messages itself, within your active hours, spacing and budget."

const SWITCHABLE = new Set<Campaign['status']>(['active', 'paused'])

function contacts(n: number): string {
  return n === 1 ? '1 contact' : `${n} contacts`
}

export function StepModeSwitch({ campaign, step }: { campaign: Campaign; step: Step }) {
  const queryClient = useQueryClient()
  const [confirming, setConfirming] = useState(false)
  const toPrefill = useMutation({
    mutationFn: () => setStepMode(campaign.id, step.id, 'prefill'),
    onSuccess: (done) => {
      queryClient.setQueryData(campaignKeys.one(campaign.id), done.campaign)
      void queryClient.invalidateQueries({ queryKey: campaignKeys.all })
    },
  })
  if (step.channel !== 'linkedin' || !SWITCHABLE.has(campaign.status)) return null
  const auto = step.mode === 'auto_send'
  return (
    <span className="mt-1 block">
      {auto ? (
        <Button
          variant="outline"
          size="sm"
          aria-label={`Switch step ${step.position} to Prefill`}
          disabled={toPrefill.isPending}
          onClick={() => toPrefill.mutate()}
        >
          Switch to Prefill
        </Button>
      ) : (
        <Button
          variant="outline"
          size="sm"
          aria-label={`Switch step ${step.position} to Auto-send`}
          onClick={() => setConfirming(true)}
        >
          Switch to Auto-send
        </Button>
      )}
      {(step.kept_prefill ?? 0) > 0 && auto && (
        <span className="block text-xs">
          {contacts(step.kept_prefill ?? 0)} already queued when it switched{' '}
          {(step.kept_prefill ?? 0) === 1 ? 'stays' : 'stay'} on Prefill.
        </span>
      )}
      {toPrefill.isError && <ErrorNote label="The mode did not change." error={toPrefill.error} />}
      {confirming && (
        <AutoSendConfirm campaign={campaign} step={step} onClose={() => setConfirming(false)} />
      )}
    </span>
  )
}

function AutoSendConfirm({
  campaign,
  step,
  onClose,
}: {
  campaign: Campaign
  step: Step
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const preview = useQuery(stepModeQuery(campaign.id, step.id))
  const save = useMutation({
    mutationFn: () => setStepMode(campaign.id, step.id, 'auto_send', true),
    onSuccess: (done) => {
      queryClient.setQueryData(campaignKeys.one(campaign.id), done.campaign)
      void queryClient.invalidateQueries({ queryKey: campaignKeys.all })
      onClose()
    },
  })
  const p = preview.data
  const refusal = p?.refusal ?? null
  return (
    <ConfirmDialog
      open
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
      title={`Switch step ${step.position} to Auto-send?`}
      confirmLabel="Switch to Auto-send"
      confirmVariant="default"
      confirmDisabled={p === undefined || refusal !== null}
      onConfirm={() =>
        p === undefined || refusal !== null ? Promise.resolve() : save.mutateAsync()
      }
      pending={save.isPending || preview.isPending}
      error={
        save.isError
          ? errorText(save.error)
          : (refusal ?? (preview.isError ? errorText(preview.error) : null))
      }
    >
      <p className="text-foreground">{p?.confirm_text ?? AUTO_SEND_CONFIRM}</p>
      {p !== undefined && (
        <>
          <p>
            {p.queued === 0
              ? 'No contacts are waiting for this step now.'
              : `${contacts(p.queued)} already waiting for this step ${p.queued === 1 ? 'stays' : 'stay'} on Prefill: you prefill ${p.queued === 1 ? 'it' : 'them'} as before.`}{' '}
          </p>
          <p className="text-foreground">
            {p.scheduled === 0
              ? 'No other contacts are scheduled for this step yet.'
              : `${contacts(p.scheduled)} already scheduled for this step will be sent on ${p.scheduled === 1 ? 'its' : 'their'} own.`}{' '}
            Contacts who reach this step later are sent on their own too.
          </p>
          {p.prefilled > 0 && (
            <p>
              {p.prefilled === 1
                ? '1 message already typed into LinkedIn keeps waiting for you.'
                : `${p.prefilled} messages already typed into LinkedIn keep waiting for you.`}
            </p>
          )}
          <p>Switching sends nothing by itself. You can switch back to Prefill at any time.</p>
          {!p.auto_send_on && (
            <Callout tone="warning" title="Auto-send is off">
              <p>
                Auto-send is turned off in your configuration file, so this step sends nothing on
                its own until you turn it on. Until then, you prefill its messages.
              </p>
            </Callout>
          )}
        </>
      )}
    </ConfirmDialog>
  )
}
