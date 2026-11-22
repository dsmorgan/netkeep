import { Button, buttonVariants } from '@/components/ui/button'
import type { Mailbox, MailboxArmSteps } from '@/features/mailboxes/api'

import { armSendBlocked, MOVE_TO_MANUAL, sendText, waitingText } from './arm-path-text'

interface ArmPathProps {
  mailbox: Mailbox
  steps: MailboxArmSteps
  onCheckNow: () => void
  checking: boolean
}

/**
 * The steps to Gmail Automatic for one mailbox (#510): Gmail on Manual, then netkeep
 * finding one of its own drafts by Message-ID, then a person moving the switch to
 * Automatic (#829; the switch is the only way there, #834). Each step shows its state,
 * and while Automatic is unavailable it says why and offers the next step.
 */
export function ArmPath({ mailbox, steps, onCheckNow, checking }: ArmPathProps) {
  const blocked = armSendBlocked(mailbox, steps)
  const found = steps.verified_at !== null
  const waitingOnServe =
    !steps.checks_running &&
    !found &&
    (steps.waiting === 'not_checked' || steps.waiting === 'not_found')
  return (
    <section aria-label="Steps to Automatic" className="space-y-2 rounded-md bg-muted/40 px-3 py-2">
      <h4 className="text-xs font-medium">Steps to Automatic</h4>
      <ol className="list-decimal space-y-1 pl-5 text-xs">
        <li>
          <span className="font-medium">Gmail on Manual.</span>{' '}
          <span className="text-muted-foreground">
            {steps.armed_for_drafts ? 'Done.' : `Not yet. ${MOVE_TO_MANUAL}`}
          </span>
        </li>
        <li>
          <span className="font-medium">netkeep found its own draft.</span>{' '}
          <span className="text-muted-foreground">{waitingText(steps)}</span>
          {!found && steps.check_requested_at !== null && (
            <span className="text-muted-foreground"> A check runs within a minute.</span>
          )}
          {waitingOnServe && (
            <span className="text-muted-foreground">
              {' '}
              netkeep serve isn’t running, so nothing checks for the draft until you start it.
            </span>
          )}
        </li>
        <li>
          <span className="font-medium">Gmail on Automatic.</span>{' '}
          <span className="text-muted-foreground">{sendText(steps)}</span>
        </li>
      </ol>
      {blocked !== null && (
        <div className="flex flex-wrap items-center gap-2">
          <p className="text-xs">{blocked}</p>
          {steps.next_step === 'send_test' && (
            <a
              className={buttonVariants({ variant: 'outline', size: 'sm' })}
              href={
                steps.test_campaign_id === null
                  ? '/campaigns'
                  : `/campaigns/${steps.test_campaign_id}`
              }
            >
              Send a test
            </a>
          )}
          {steps.next_step === 'check_now' && (
            <Button variant="outline" size="sm" onClick={onCheckNow} disabled={checking}>
              {checking ? 'Checking…' : 'Check now'}
            </Button>
          )}
        </div>
      )}
      <p className="text-xs text-muted-foreground">
        netkeep finds each message again by the Message-ID it sets, so before it sends on its own,
        it checks that Gmail keeps that Message-ID. On Automatic, netkeep serve sends campaign email
        from this mailbox with no one pressing Send. Until then, on Manual, every step is a draft
        you send yourself. Off takes all of that back: from the next minute, netkeep makes no drafts
        and sends nothing until you move Gmail to Manual again.
      </p>
    </section>
  )
}
