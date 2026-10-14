import { Button, buttonVariants } from '@/components/ui/button'
import type { Mailbox, MailboxArmSteps } from '@/features/mailboxes/api'

import { armSendBlocked, sendText, waitingText } from './arm-path-text'

interface ArmPathProps {
  mailbox: Mailbox
  steps: MailboxArmSteps
  onCheckNow: () => void
  checking: boolean
  /** The id of the paragraph that says why Arm to send is off, for its aria-describedby. */
  blockedId: string
}

/**
 * The way to arming a mailbox to send (#510): armed for drafts, then netkeeper finding
 * one of its own drafts by Message-ID, then a person arming it to send. Each step shows
 * its state, and a blocked Arm to send says why and offers the next step.
 */
export function ArmPath({ mailbox, steps, onCheckNow, checking, blockedId }: ArmPathProps) {
  const blocked = armSendBlocked(mailbox, steps)
  const found = steps.verified_at !== null
  const waitingOnServe =
    !steps.checks_running &&
    !found &&
    (steps.waiting === 'not_checked' || steps.waiting === 'not_found')
  return (
    <section aria-label="Arm to send" className="space-y-2 rounded-md bg-muted/40 px-3 py-2">
      <h4 className="text-xs font-medium">Steps to arm to send</h4>
      <ol className="list-decimal space-y-1 pl-5 text-xs">
        <li>
          <span className="font-medium">Armed for drafts.</span>{' '}
          <span className="text-muted-foreground">
            {steps.armed_for_drafts ? 'Done.' : 'Not yet. Use Arm for drafts.'}
          </span>
        </li>
        <li>
          <span className="font-medium">netkeeper found its own draft.</span>{' '}
          <span className="text-muted-foreground">{waitingText(steps)}</span>
          {!found && steps.check_requested_at !== null && (
            <span className="text-muted-foreground"> A check runs within a minute.</span>
          )}
          {waitingOnServe && (
            <span className="text-muted-foreground">
              {' '}
              netkeeper serve isn’t running, so nothing checks for the draft until you start it.
            </span>
          )}
        </li>
        <li>
          <span className="font-medium">Arm to send.</span>{' '}
          <span className="text-muted-foreground">{sendText(steps)}</span>
        </li>
      </ol>
      {blocked !== null && (
        <div className="flex flex-wrap items-center gap-2">
          <p id={blockedId} className="text-xs">
            {blocked}
          </p>
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
        netkeeper finds each message again by the Message-ID it sets, so before it sends on its own,
        it checks that Gmail keeps that Message-ID. Arm to send lets netkeeper serve send campaign
        email from this mailbox with no one pressing Send. Until then, every step is a draft you
        send yourself. Disarm takes all of that back: from the next minute, netkeeper makes no
        drafts and sends nothing until you arm the mailbox again, starting with drafts.
      </p>
    </section>
  )
}
