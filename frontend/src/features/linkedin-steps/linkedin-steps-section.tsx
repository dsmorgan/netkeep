/**
 * The LinkedIn queue and what waits for you (#383, spec 11.6), on the dashboard and
 * on a campaign's page.
 *
 * - **The queue** lists due LinkedIn steps. A person clicks **Prefill** (or **Prefill
 *   next**) to start one; nothing prefills on its own. While it types: "Typing in
 *   Chrome…", with the run's live progress. A refusal says why, and that nothing was
 *   typed.
 * - **Waiting for you** lists prefilled messages to send by hand in Chrome, stale ones
 *   (highlighted), and prefills that stopped before they said what they typed. Each
 *   offers **I sent it, check now** (an inbox poll) and **Discard**.
 * - **One prefill at a time:** while one is typing or open, every Prefill button is
 *   off, and the queue says why.
 * - **Try again** (#445): a step whose last prefill typed nothing (`not_typed`) is listed
 *   apart, with why, how many tries, and whether the last one used today's budget. A
 *   person clicks Try again for one; when the last try clicked Message, it first asks
 *   them to confirm no message bubble for that contact is open in Chrome.
 * - **Auto-send** (ADR 0008): an `auto_send` step is marked in the queue, and says
 *   whether `[campaigns] linkedin_auto_send` is on. Settings (with a confirmation) or
 *   `config.toml` turns it on; nothing here does. A stale message auto-send clicked Send for says so.
 *
 * A part-typed prefill is never retried: a person clears the composer in Chrome and
 * discards it. No message text is shown anywhere here; netkeep never closes the tab.
 */
import { Link } from '@tanstack/react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type ComponentProps } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { attentionQuery } from '@/features/attention/api'
import { campaignKeys } from '@/features/campaigns/api'
import { formatWhen } from '@/features/campaigns/format'
import { Callout, ErrorNote, LoadingNote } from '@/features/crm/controls'
import { linkedinKeys, runQuery, scheduleQuery } from '@/features/linkedin/api'
import { stopReasonLabel, summarizeFields } from '@/features/linkedin/fields'
import { useNow } from '@/features/poll-status/use-now'
import { cn } from '@/lib/utils'

import {
  LinkedInStepError,
  checkSent,
  discard,
  linkedinStepKeys,
  prefill,
  readyQuery,
  resumeAutoSend,
  stepOptionsQuery,
  waitingQuery,
  type PrefillTarget,
  type ReadyItem,
  type TryAgainItem,
  type WaitingItem,
} from './api'
import {
  AUTO_SEND_ON_NOTE,
  AUTO_SEND_STEP_OFF,
  AUTO_SENT_STALE,
  ONE_AT_A_TIME,
  PREFILL_IT_YOURSELF,
  REVIEW_AND_SEND,
  WAITS_FOR_PREFILL,
  ageText,
  autoSendStepText,
  heldText,
  openPrefill,
  reasonText,
  waitingState,
  type AutoSendOutlook,
  type WaitingState,
} from './format'
import {
  CONFIRM_BUBBLE_BODY,
  CONFIRM_BUBBLE_LABEL,
  PARTLY_TYPED,
  PARTLY_TYPED_SENT_ANYWAY,
  PREFILL_NOTE,
  TRY_AGAIN_NOTE,
  AUTO_SENDING,
  AUTO_STAYS_DUE,
  AUTO_STOPPED,
  AUTO_TYPED_NOT_SENT,
  AUTO_UNKNOWN,
  AUTO_SENDING_WARNING,
  AUTO_SENT,
  TYPED_WHOLE,
  TYPING,
  TYPING_WARNING,
  budgetText,
  confirmBubbleTitle,
  prefillEnding,
  prefillReason,
  prefillsLeftText,
  triesText,
} from './prefill-copy'
import { FirstPollNote } from './first-poll-note'
import { usePrefillRun, useRunGoing, type PrefillRun } from './use-prefill-run'

/** A card's heading level: 3 under a region with a heading of its own, such as Needs you. */
type Level = 2 | 3

/**
 * The queue and the waiting list together: one campaign's when `campaignId` is given.
 * `level` is the cards' heading level (2 on a page of cards, 3 inside the dashboard's
 * Needs you), and the queue's own subheading goes one below it.
 */
export function LinkedInStepsSection({
  campaignId,
  level = 2,
}: {
  campaignId?: number
  level?: Level
}) {
  const run = usePrefillRun()
  return (
    <>
      <LinkedInQueueCard campaignId={campaignId} run={run} level={level} />
      <WaitingForYouCard campaignId={campaignId} level={level} />
    </>
  )
}

/** A heading one level below its card's title. */
function Subheading({ level, ...props }: ComponentProps<'h3'> & { level: Level }) {
  const Heading = level === 2 ? 'h3' : 'h4'
  return <Heading {...props} />
}

function useInvalidateSteps() {
  const queryClient = useQueryClient()
  return () => {
    void queryClient.invalidateQueries({ queryKey: linkedinStepKeys.all })
    void queryClient.invalidateQueries({ queryKey: campaignKeys.all })
  }
}

/**
 * Auto-send is held (ADR 0008): an auto-send left a message bubble open in Chrome, so
 * nothing else is sent until you close it and resume.
 */
function AutoSendHold() {
  const options = useQuery(stepOptionsQuery)
  const queryClient = useQueryClient()
  const resume = useMutation({
    mutationFn: resumeAutoSend,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: linkedinStepKeys.all })
    },
  })
  const hold = options.data?.auto_send_hold
  if (hold == null) return null
  return (
    <Callout tone="warning" role="alert" title={`Auto-send is held: ${hold.reason}.`}>
      <p>{hold.how_to_clear}</p>
      <p className="text-muted-foreground">Held since {formatWhen(hold.since)}.</p>
      <Button
        size="sm"
        className="mt-2"
        disabled={resume.isPending}
        onClick={() => resume.mutate()}
      >
        I fixed it in Chrome, resume auto-send
      </Button>
      {resume.isError && <ErrorNote label="Auto-send did not resume." error={resume.error} />}
    </Callout>
  )
}

function AutoSendBadge() {
  const options = useQuery(stepOptionsQuery)
  if (options.isPending) return null
  if (options.isError) {
    return (
      <p className="flex flex-wrap items-center gap-2">
        <Badge variant="outline">Auto-send: unknown</Badge>
        <span className="text-muted-foreground">
          netkeep could not read the setting; Settings, Posture shows it.
        </span>
      </p>
    )
  }
  // A standing setting, not an event: a highlighted note, never an alert.
  return options.data.auto_send ? (
    <p
      role="note"
      className="flex flex-wrap items-center gap-2 rounded-md border border-destructive/50 bg-bad-soft px-2 py-1 text-destructive"
    >
      <Badge variant="destructive">Auto-send on</Badge>
      {AUTO_SEND_ON_NOTE}
      {options.data.auto_send_warning != null && (
        <span className="block w-full">{options.data.auto_send_warning}</span>
      )}
    </p>
  ) : (
    <p className="flex flex-wrap items-center gap-2">
      <span className="inline-flex h-5 items-center rounded-4xl bg-ok-soft px-2 text-xs font-medium text-ok-ink ring-1 ring-ok/40">
        Auto-send off
      </span>
      <span className="text-muted-foreground">You send every LinkedIn message yourself.</span>
    </p>
  )
}

export function LinkedInQueueCard({
  campaignId,
  run,
  level = 2,
}: {
  campaignId?: number
  run: PrefillRun
  level?: Level
}) {
  const ready = useQuery(readyQuery(campaignId ?? null))
  const options = useQuery(stepOptionsQuery)
  const autoSendOn = options.data?.auto_send === true
  // When the next auto-send check would send, or why it waits: the dashboard's own read.
  const schedule = useQuery({ ...scheduleQuery, enabled: autoSendOn })
  const outlook = schedule.data?.auto_send ?? null
  // Every campaign's: one open prefill anywhere holds the slot.
  const waiting = useQuery(waitingQuery())
  const invalidate = useInvalidateSteps()
  const queryClient = useQueryClient()

  const start = useMutation({
    mutationFn: (target: PrefillTarget | 'next') => prefill(target),
    onSuccess: (accepted) => {
      run.watch(accepted.run_id)
      invalidate()
    },
    onError: () => {
      // A refusal can still change an enrollment (a reply ended it, a step parked).
      invalidate()
      void queryClient.invalidateQueries({ queryKey: linkedinKeys.status() })
    },
  })

  // Ticks with the clock, so a hold that lapses frees Prefill without a refetch (#704).
  const now = new Date(useNow(30_000))
  const open = waiting.isSuccess ? openPrefill(waiting.data.items, now) : null
  const typing = run.runningId !== null || start.isPending
  const autoSending = run.autoSendId !== null
  const blocked = typing || autoSending || open !== null || !waiting.isSuccess
  const items = ready.data?.items ?? []
  const retries = ready.data?.try_again ?? []
  // The ready page lists at most 50 of them; the dashboard's count says how many wait
  // in all (#508). A campaign page lists its own campaign's, which that count is not.
  const attention = useQuery({ ...attentionQuery, enabled: campaignId === undefined })
  const retriesTotal = campaignId === undefined ? (attention.data?.counts.try_again ?? 0) : 0
  const nextReady = items.find((item) => !held(item, now))

  return (
    <Card>
      <CardHeader>
        <CardTitle level={level}>LinkedIn queue</CardTitle>
        <CardDescription>
          Due LinkedIn steps. Each row says whether it waits for you to prefill it or sends on its
          own. When you prefill one, watch Chrome, and then click Send yourself.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 text-sm">
        <AutoSendBadge />
        <AutoSendHold />
        <FirstPollNote />
        {autoSending && (
          <div role="status" className="rounded-lg border border-primary/40 bg-accent-soft p-3">
            <p className="font-medium">{AUTO_SENDING}</p>
            <p>{AUTO_SENDING_WARNING}</p>
          </div>
        )}
        {!typing && !autoSending && <p className="text-muted-foreground">{PREFILL_NOTE}</p>}
        {typing && (
          <div role="status" className="rounded-lg border border-primary/40 bg-accent-soft p-3">
            <p className="font-medium">{TYPING}</p>
            <p>{TYPING_WARNING}</p>
            {run.progress !== null && (
              <p className="text-muted-foreground">{summarizeFields(run.progress, 6)}</p>
            )}
          </div>
        )}
        {!typing && !autoSending && run.finishedId !== null && (
          <FinishedPrefill runId={run.finishedId} onDismiss={run.dismiss} />
        )}
        {start.isError && <Refused error={start.error} onDismiss={() => start.reset()} />}
        {!typing && open !== null && <p className="text-muted-foreground">{ONE_AT_A_TIME}</p>}
        {ready.isSuccess &&
          ready.data.prefills_left_today !== null &&
          ready.data.prefills_left_today !== undefined && (
            <p className="text-muted-foreground">
              {prefillsLeftText(ready.data.prefills_left_today)}
            </p>
          )}

        {retries.length > 0 && (
          <section aria-label="Try again" className="flex flex-col gap-1">
            <Subheading level={level} className="font-medium">
              Typed nothing last time
            </Subheading>
            <p className="text-muted-foreground">{TRY_AGAIN_NOTE}</p>
            <ul aria-label="Try again" className="flex flex-col">
              {retries.map((item) => (
                <TryAgainRow
                  key={item.enrollment_id}
                  item={item}
                  showCampaign={campaignId === undefined}
                  disabled={blocked || held(item, now)}
                  onRetry={(noBubbleOpen) =>
                    start.mutateAsync({
                      enrollmentId: item.enrollment_id,
                      retry: true,
                      noBubbleOpen,
                    })
                  }
                />
              ))}
            </ul>
            {retries.length >= TRY_AGAIN_MAX && retriesTotal > retries.length && (
              <p className="text-muted-foreground">
                Showing {retries.length} of {retriesTotal}, oldest try first.
              </p>
            )}
          </section>
        )}

        {ready.isPending ? (
          <LoadingNote label="Loading the queue…" />
        ) : ready.isError ? (
          <ErrorNote label="The LinkedIn queue is unavailable." error={ready.error} />
        ) : items.length === 0 ? (
          <p className="text-muted-foreground">No LinkedIn step is due.</p>
        ) : (
          <>
            <ul aria-label="Ready to prefill" className="flex flex-col">
              {items.map((item) => (
                <ReadyRow
                  key={item.enrollment_id}
                  item={item}
                  autoSendOn={autoSendOn}
                  outlook={outlook}
                  now={now}
                  showCampaign={campaignId === undefined}
                  disabled={blocked || held(item, now)}
                  onPrefill={() => start.mutate({ enrollmentId: item.enrollment_id })}
                />
              ))}
            </ul>
            <div className="flex flex-wrap items-center gap-2">
              {campaignId === undefined && (
                <Button
                  disabled={blocked || nextReady === undefined}
                  onClick={() => start.mutate('next')}
                >
                  Prefill next
                </Button>
              )}
              {ready.data.total > items.length && (
                <span className="text-muted-foreground">
                  Showing {items.length} of {ready.data.total}, oldest due first.
                </span>
              )}
            </div>
          </>
        )}
      </CardContent>
    </Card>
  )
}

/** The most Try again rows `GET /campaigns/linkedin/ready` lists; mirrors the backend's
 * `READY_PAGE_MAX` in `services/linkedin_steps.py`. */
const TRY_AGAIN_MAX = 50

/** Held by the sending hours: due, but not until `held_until`. */
function held(item: ReadyItem | TryAgainItem, now: Date): boolean {
  return item.held_until !== null && new Date(item.held_until).getTime() > now.getTime()
}

function ReadyRow({
  item,
  autoSendOn,
  outlook,
  now,
  showCampaign,
  disabled,
  onPrefill,
}: {
  item: ReadyItem
  autoSendOn: boolean
  outlook: AutoSendOutlook | null
  now: Date
  showCampaign: boolean
  disabled: boolean
  onPrefill: () => void
}) {
  const name = item.contact_name || 'Unnamed contact'
  return (
    <li
      aria-label={`${name}, step ${item.step_position}`}
      className="flex flex-wrap items-center gap-2 border-t border-border/60 py-2 first:border-t-0"
    >
      <Link
        to="/contacts/$contactId"
        params={{ contactId: String(item.contact_id) }}
        className="font-medium underline underline-offset-4"
      >
        {name}
      </Link>
      {showCampaign && (
        <>
          <span className="text-muted-foreground">in</span>
          <Link
            to="/campaigns/$campaignId"
            params={{ campaignId: String(item.campaign_id) }}
            className="underline underline-offset-4"
          >
            {item.campaign_name}
          </Link>
        </>
      )}
      <span className="text-muted-foreground">step {item.step_position}</span>
      {item.auto_send ? (
        <span className="flex flex-wrap items-center gap-2">
          <Badge variant={autoSendOn ? 'destructive' : 'outline'}>
            {autoSendOn ? 'Auto-send' : 'Auto-send step'}
          </Badge>
          <span className="text-muted-foreground">
            {autoSendOn
              ? `${autoSendStepText(outlook, item, now)} ${PREFILL_IT_YOURSELF}`
              : AUTO_SEND_STEP_OFF}
          </span>
        </span>
      ) : (
        <span className="text-muted-foreground">{WAITS_FOR_PREFILL}</span>
      )}
      {item.held_until !== null && (
        <span className="text-muted-foreground">{heldText(item.held_until)}</span>
      )}
      <Button
        size="sm"
        className="ml-auto"
        disabled={disabled}
        aria-label={`Prefill ${name}`}
        onClick={onPrefill}
      >
        Prefill
      </Button>
    </li>
  )
}

/**
 * A step whose last prefill typed nothing (#445): why, how many tries, whether that try
 * used today's budget, and **Try again**. When the last try clicked Message (or nothing
 * says it didn't), Try again first asks you to confirm that no message bubble for the
 * contact is open in Chrome; the backend refuses a retry without that confirmation.
 */
function TryAgainRow({
  item,
  showCampaign,
  disabled,
  onRetry,
}: {
  item: TryAgainItem
  showCampaign: boolean
  disabled: boolean
  onRetry: (noBubbleOpen: boolean) => Promise<unknown>
}) {
  const [confirming, setConfirming] = useState(false)
  const name = item.contact_name || 'Unnamed contact'
  const last = item.last_try
  const retry = (noBubbleOpen: boolean) =>
    onRetry(noBubbleOpen).catch(() => {
      // The queue's own alert says why; nothing to add here.
    })
  return (
    <li
      aria-label={`${name}, step ${item.step_position}, typed nothing`}
      className="flex flex-col gap-1 border-t border-border/60 py-2 first:border-t-0"
    >
      <div className="flex flex-wrap items-center gap-2">
        <Link
          to="/contacts/$contactId"
          params={{ contactId: String(item.contact_id) }}
          className="font-medium underline underline-offset-4"
        >
          {name}
        </Link>
        {showCampaign && (
          <>
            <span className="text-muted-foreground">in</span>
            <Link
              to="/campaigns/$campaignId"
              params={{ campaignId: String(item.campaign_id) }}
              className="underline underline-offset-4"
            >
              {item.campaign_name}
            </Link>
          </>
        )}
        <span className="text-muted-foreground">step {item.step_position}</span>
        <Badge variant="outline">Typed nothing</Badge>
        <Button
          size="sm"
          variant="outline"
          className="ml-auto"
          disabled={disabled}
          aria-label={`Try again for ${name}`}
          onClick={() => {
            if (last.needs_confirmation) setConfirming(true)
            else void retry(false)
          }}
        >
          Try again
        </Button>
      </div>
      <p>{prefillReason(last.reason).text}</p>
      <p className="text-muted-foreground">
        {triesText(last.tries)} {budgetText(last)}
        {last.at !== null && ` Last try ${formatWhen(last.at)}.`}
      </p>
      {item.held_until !== null && (
        <p className="text-muted-foreground">{heldText(item.held_until)}</p>
      )}
      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title={confirmBubbleTitle(name)}
        confirmLabel={CONFIRM_BUBBLE_LABEL}
        confirmVariant="default"
        onConfirm={async () => {
          setConfirming(false)
          await retry(true)
        }}
      >
        <p>{CONFIRM_BUBBLE_BODY}</p>
      </ConfirmDialog>
    </li>
  )
}

/** The statuses that mean the backend answered and claimed nothing (no run, no typing). */
const REFUSED_STATUSES = new Set([404, 409, 503])

/**
 * Why a prefill did not start. Only an answer the backend gave (404, 409, 503) proves
 * nothing was typed; any other failure (the network, a timeout, a 5xx) leaves it
 * unknown, so it says to look before trying again.
 */
function Refused({ error, onDismiss }: { error: unknown; onDismiss: () => void }) {
  const answered = error instanceof LinkedInStepError && REFUSED_STATUSES.has(error.status)
  const refusal = answered ? error.refusal : null
  return (
    <Callout
      tone="warning"
      role="alert"
      title={
        answered
          ? 'Not prefilled. Nothing was typed in Chrome.'
          : 'The prefill request failed. Check Waiting for you and the LinkedIn page before you try again.'
      }
    >
      {refusal !== null ? (
        <>
          <ul className="list-disc pl-5">
            {refusal.reasons.map((reason) => (
              <li key={reason}>{reasonText(reason)}</li>
            ))}
          </ul>
          {refusal.detail !== null && <p className="text-muted-foreground">{refusal.detail}</p>}
        </>
      ) : (
        <p>{error instanceof Error ? error.message : 'The prefill could not start.'}</p>
      )}
      <Button size="sm" variant="outline" className="mt-2" onClick={onDismiss}>
        Dismiss
      </Button>
    </Callout>
  )
}

/** How the last prefill run ended, when it ended without a prefilled message. */
function FinishedPrefill({ runId, onDismiss }: { runId: number; onDismiss: () => void }) {
  const run = useQuery(runQuery(runId))
  if (!run.isSuccess || run.data.status === 'running') return null
  if (run.data.stop_reason === 'send_clicked') {
    return (
      <p role="status" className="text-muted-foreground">
        {AUTO_SENT}{' '}
        <Button size="sm" variant="ghost" onClick={onDismiss}>
          Dismiss
        </Button>
      </p>
    )
  }
  const auto = run.data.trigger === 'scheduled'
  if (auto && run.data.stop_reason === 'unknown') {
    // Send may have been clicked: never the prefill copy that says to clear the composer.
    return (
      <Callout tone="warning" role="alert" title="Check whether it was sent.">
        <p>{AUTO_UNKNOWN}</p>
        <Button size="sm" variant="outline" className="mt-2" onClick={onDismiss}>
          Dismiss
        </Button>
      </Callout>
    )
  }
  if (run.data.status === 'completed') {
    return (
      <p role="status" className="text-muted-foreground">
        {auto ? AUTO_TYPED_NOT_SENT : TYPED_WHOLE}{' '}
        <Button size="sm" variant="ghost" onClick={onDismiss}>
          Dismiss
        </Button>
      </p>
    )
  }
  // An auto-send that opened nothing is given back due and retried by itself, unless a
  // person cancelled it: that parks the step for Try again (#708).
  if (
    auto &&
    run.data.counts?.opened === false &&
    run.data.cancel_requested_at === null &&
    run.data.stop_reason === 'not_typed'
  ) {
    const why = prefillReason(run.data.error)
    return (
      <Callout tone="warning" role="alert" title={AUTO_STOPPED}>
        {why.text !== null && <p>{why.text}</p>}
        <p>{AUTO_STAYS_DUE}</p>
        <Button size="sm" variant="outline" className="mt-2" onClick={onDismiss}>
          Dismiss
        </Button>
      </Callout>
    )
  }
  const ending = prefillEnding(run.data.stop_reason, run.data.error, run.data.counts)
  if (ending !== null) {
    return (
      <Callout tone="warning" role="alert" title={auto ? AUTO_STOPPED : ending.title}>
        {ending.reason !== null && <p>{ending.reason}</p>}
        {ending.steps.map((step) => (
          <p key={step}>{step}</p>
        ))}
        <Button size="sm" variant="outline" className="mt-2" onClick={onDismiss}>
          Dismiss
        </Button>
      </Callout>
    )
  }
  const reason = stopReasonLabel(run.data) ?? run.data.error ?? 'no reason recorded'
  return (
    <Callout tone="warning" title={auto ? AUTO_STOPPED : 'The prefill stopped.'}>
      <p>{reason}.</p>
      <p>
        If anything was typed, clear the composer in Chrome yourself. netkeep never retries a
        prefill.
      </p>
      <Button size="sm" variant="outline" className="mt-2" onClick={onDismiss}>
        Dismiss
      </Button>
    </Callout>
  )
}

const STATE_TEXT: Record<WaitingState, string> = {
  prefilled: REVIEW_AND_SEND,
  stale:
    'Stale: prefilled three days ago or more. The tab stays open; send it, or discard it to move on.',
  interrupted:
    'The prefill stopped before it recorded what it typed. Check the composer in Chrome, clear it, then discard this.',
  partly_typed: PARTLY_TYPED,
}

export function WaitingForYouCard({
  campaignId,
  level = 2,
}: {
  campaignId?: number
  level?: Level
}) {
  const waiting = useQuery(waitingQuery(campaignId ?? null))
  const now = new Date(useNow(30_000))
  const items = waiting.data?.items ?? []
  return (
    // The send tray: tinted while messages wait for you to send them.
    <Card tone={items.length > 0 ? 'accent' : undefined} tray>
      <CardHeader>
        <CardTitle level={level}>Waiting for you</CardTitle>
        <CardDescription>
          LinkedIn messages typed in Chrome for you to send, or to discard.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col text-sm">
        {waiting.isPending ? (
          <LoadingNote label="Loading what waits for you…" />
        ) : waiting.isError ? (
          <ErrorNote label="What waits for you is unavailable." error={waiting.error} />
        ) : items.length === 0 ? (
          <p className="text-muted-foreground">Nothing waits for you.</p>
        ) : (
          <>
            <ul aria-label="Waiting for you" className="flex flex-col gap-2">
              {items.map((item) => (
                <WaitingRow
                  key={item.message_id}
                  item={item}
                  state={waitingState(item, now)}
                  now={now}
                  showCampaign={campaignId === undefined}
                />
              ))}
            </ul>
            {waiting.data.total > items.length && (
              <p className="pt-2 text-muted-foreground">
                Showing {items.length} of {waiting.data.total}, oldest first.
              </p>
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}

function WaitingRow({
  item,
  state,
  now,
  showCampaign,
}: {
  item: WaitingItem
  state: WaitingState
  now: Date
  showCampaign: boolean
}) {
  const invalidate = useInvalidateSteps()
  const [confirming, setConfirming] = useState(false)
  const check = useMutation({ mutationFn: () => checkSent(item.message_id) })
  const checking = useRunGoing(check.data?.run_id ?? null)
  const drop = useMutation({
    mutationFn: () => discard(item.message_id),
    onSuccess: () => {
      setConfirming(false)
      invalidate()
    },
  })
  const name = item.contact_name || 'Unnamed contact'

  return (
    <li
      aria-label={`${name}, ${state}`}
      data-state={state}
      className={cn(
        'flex flex-col gap-1.5 rounded-lg border p-3',
        state === 'stale' && 'border-warn/60 bg-warn-soft',
        (state === 'interrupted' || state === 'partly_typed') &&
          'border-destructive/40 bg-bad-soft',
        state === 'partly_typed' && 'border-destructive bg-bad-soft',
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Link
          to="/contacts/$contactId"
          params={{ contactId: String(item.contact_id) }}
          className="font-medium underline underline-offset-4"
        >
          {name}
        </Link>
        {showCampaign && (
          <>
            <span className="text-muted-foreground">in</span>
            <Link
              to="/campaigns/$campaignId"
              params={{ campaignId: String(item.campaign_id) }}
              className="underline underline-offset-4"
            >
              {item.campaign_name}
            </Link>
          </>
        )}
        {state === 'stale' && <Badge variant="outline">Stale</Badge>}
        {item.auto_sent && <Badge variant="destructive">Auto-sent</Badge>}
        {state === 'interrupted' && <Badge variant="destructive">Stopped</Badge>}
        {state === 'partly_typed' && <Badge variant="destructive">Part typed</Badge>}
        {state === 'prefilled' || state === 'stale' ? (
          <span className="ml-auto text-muted-foreground">
            Prefilled {ageText(item.prefilled_at, now)}
          </span>
        ) : null}
      </div>
      <p>{item.auto_sent && state === 'stale' ? AUTO_SENT_STALE : STATE_TEXT[state]}</p>
      {item.not_sent_reason != null && (
        <p className="text-muted-foreground">
          Auto-send typed it but didn&apos;t click Send: {prefillReason(item.not_sent_reason).text}
        </p>
      )}
      {state === 'partly_typed' && <p>{PARTLY_TYPED_SENT_ANYWAY}</p>}
      <div className="flex flex-wrap items-center gap-2">
        {(state === 'prefilled' || state === 'stale') && (
          <Button size="sm" disabled={check.isPending || checking} onClick={() => check.mutate()}>
            I sent it, check now
          </Button>
        )}
        <Button
          size="sm"
          variant={state === 'partly_typed' ? 'default' : 'outline'}
          onClick={() => setConfirming(true)}
        >
          {state === 'partly_typed' ? 'I cleared it, discard' : 'Discard'}
        </Button>
      </div>
      {check.isSuccess && <CheckStarted runId={check.data.run_id} />}
      {check.isError && <ErrorNote label="The inbox check did not start." error={check.error} />}
      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title={`Discard the message to ${name}?`}
        confirmLabel={state === 'partly_typed' ? 'I cleared it, discard' : 'Discard'}
        onConfirm={() => drop.mutateAsync()}
        pending={drop.isPending}
        error={drop.isError ? drop.error.message : null}
      >
        <p>
          The step counts as fired and the enrollment moves to its next step. netkeep changes
          nothing in LinkedIn: if the message is still in the composer, clear it in Chrome yourself.
        </p>
      </ConfirmDialog>
    </li>
  )
}

/** The inbox check a person asked for: running, or how it ended. */
function CheckStarted({ runId }: { runId: number }) {
  const run = useQuery(runQuery(runId))
  if (!run.isSuccess || run.data.status === 'running') {
    return (
      <p role="status" className="text-muted-foreground">
        Checking your LinkedIn inbox. The message is marked sent once the check finds it.
      </p>
    )
  }
  if (run.data.status === 'completed') {
    return (
      <p role="status" className="text-muted-foreground">
        The inbox check finished. If it found the message, it no longer waits here.
      </p>
    )
  }
  return (
    <p role="status" className="text-muted-foreground">
      The inbox check stopped: {stopReasonLabel(run.data) ?? run.data.error ?? 'no reason recorded'}
      .
    </p>
  )
}
