import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useId, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader } from '@/components/ui/card'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { renderInlineMarkdown } from '@/lib/inline-markdown'
import { cn } from '@/lib/utils'

import {
  FORGET_INBOX_OWNER_ACTION,
  REPLY_POLL_KEY,
  MANUAL_SENDS_KEY,
  RESET_BREAKER_ACTION,
  acknowledgeInboxFirstPoll,
  forgetInboxOwner,
  postureQuery,
  resetBreaker,
  type Protection,
} from './api'

const STATUS_CLASSES: Record<string, string> = {
  on: 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
  off: 'bg-destructive/10 text-destructive',
  unknown: 'bg-amber-500/15 text-amber-800 dark:text-amber-300',
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * Every protection the LinkedIn extractor has, right now (spec section 9,
 * P2-11) — the same report `netkeeper posture` prints from a terminal, read
 * here instead of typed. Read-only and built with no browser probe, so the
 * `linkedin session` row always reads "unknown": a live check stays
 * `netkeeper preflight`. The verdict line is shown exactly as the backend
 * sends it — "nothing is misconfigured" on a clean report, never "you are
 * safe" — because this reads configuration and counters, not whether the
 * code that would enforce them actually runs (`netkeeper posture`'s own
 * wording, spec 9, `netkeeper/services/posture.py`).
 *
 * It leads with a summary (#340): one line per protection, its state, and
 * every warning, since a warning is what makes the verdict "NOT clear". Each
 * row's full detail, its notes, and the gaps sit behind "Show details", the
 * same split as `netkeeper posture` and `netkeeper posture --details`.
 */
export function PostureSection() {
  const posture = useQuery(postureQuery)
  const [expanded, setExpanded] = useState(false)
  const [acting, setActing] = useState<RowAction | null>(null)
  const detailsId = useId()

  return (
    <Card size="sm">
      <CardHeader>
        <h2 className="font-heading text-sm leading-snug font-medium">Posture</h2>
        <CardDescription>Every protection running right now, read-only.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        {posture.isPending && <p role="status">Loading…</p>}
        {posture.isError && <p role="alert">{message(posture.error)}</p>}
        {posture.isSuccess && (
          <>
            <ManualSends rows={posture.data.protections} />
            <ReplyPollAcknowledge rows={posture.data.protections} />
            {/* Below `sm` a table has no room for a Detail column at all: a fixed
                width just wraps prose one character per line rather than making
                it any narrower (review179r2 measured a 31,093px-tall page from
                exactly that). Below `sm` every protection is its own stacked
                block instead, each with the full page width to wrap in; from
                `sm` up there is room for a table, so this renders one markup
                and hides half of it with `sm:` rather than measuring width in
                script — the same approach `run-detail.tsx`'s FieldList uses. */}
            <div id={detailsId} className="space-y-4">
              <ul className="space-y-2 sm:hidden" data-testid="posture-blocks">
                {posture.data.protections.map((row) => (
                  <ProtectionBlock
                    key={row.name}
                    row={row}
                    expanded={expanded}
                    onAction={setActing}
                  />
                ))}
              </ul>
              <table
                className="hidden w-full table-fixed text-left sm:table"
                data-testid="posture-table"
              >
                <colgroup>
                  <col className="w-40" />
                  <col className="w-20" />
                  <col />
                </colgroup>
                <thead className="text-muted-foreground">
                  <tr>
                    <th scope="col" className="py-1 pr-3 font-medium">
                      Protection
                    </th>
                    <th scope="col" className="py-1 pr-3 font-medium">
                      State
                    </th>
                    <th scope="col" className="py-1 font-medium">
                      {expanded ? 'Detail' : 'Summary'}
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {posture.data.protections.map((row) => (
                    <ProtectionRow
                      key={row.name}
                      row={row}
                      expanded={expanded}
                      onAction={setActing}
                    />
                  ))}
                </tbody>
              </table>

              {expanded && posture.data.gaps.length > 0 && (
                <div>
                  <h3 className="text-xs font-medium text-muted-foreground">
                    Not covered by this report
                  </h3>
                  <ul className="list-disc space-y-1 pl-5 text-muted-foreground">
                    {posture.data.gaps.map((gap) => (
                      <li key={gap}>{renderInlineMarkdown(gap)}</li>
                    ))}
                  </ul>
                </div>
              )}
            </div>

            <Button
              variant="outline"
              size="sm"
              aria-expanded={expanded}
              aria-controls={detailsId}
              onClick={() => setExpanded((open) => !open)}
            >
              {expanded ? 'Hide details' : 'Show details'}
            </Button>

            <p role="status" className="font-medium">
              {posture.data.verdict}
            </p>

            <RowActionDialog
              acting={acting}
              rows={posture.data.protections}
              onClose={() => setActing(null)}
            />
          </>
        )}
      </CardContent>
    </Card>
  )
}

/**
 * ADR 0004's "manual linkedin sends" row, shown first (#383): auto-send off, in
 * plain words; on, highlighted as a warning with the backend's own warning text.
 */
function ManualSends({ rows }: { rows: readonly Protection[] }) {
  const row = rows.find((r) => r.key === MANUAL_SENDS_KEY)
  if (row === undefined) return null
  const autoSendOn = row.status !== 'on'
  return (
    <div
      role={autoSendOn ? 'alert' : 'note'}
      aria-label="Manual LinkedIn sends"
      data-auto-send={autoSendOn ? 'on' : 'off'}
      className={cn(
        'rounded-lg border-2 p-3',
        autoSendOn
          ? 'border-destructive bg-destructive/10'
          : 'border-emerald-600/50 bg-emerald-500/10',
      )}
    >
      <p className="flex flex-wrap items-center gap-2 font-medium">
        Manual LinkedIn sends <StatusBadge status={row.status} />
        <span>{autoSendOn ? 'Auto-send is ON.' : 'Auto-send is off.'}</span>
      </p>
      {autoSendOn ? (
        <ul className="mt-1 space-y-1 text-destructive">
          {row.warnings.map((warning) => (
            <li key={warning}>{renderInlineMarkdown(warning)}</li>
          ))}
        </ul>
      ) : (
        <p className="text-muted-foreground">
          netkeeper prefills each LinkedIn message for you to send yourself; it never sends one.
        </p>
      )}
    </div>
  )
}

/**
 * The warning that the first LinkedIn inbox poll could not read back far enough, and
 * **Acknowledge** for it (#383): `netkeeper linkedin inbox-acknowledge`, without the CLI.
 */
function ReplyPollAcknowledge({ rows }: { rows: readonly Protection[] }) {
  const queryClient = useQueryClient()
  const acknowledge = useMutation({
    mutationFn: acknowledgeInboxFirstPoll,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: postureQuery.queryKey }),
  })
  const row = rows.find((r) => r.key === REPLY_POLL_KEY && r.warnings.length > 0)
  if (row === undefined) {
    // Only right after an Acknowledge that found nothing; a row that comes back later
    // shows its button again.
    return acknowledge.data === false ? (
      <p role="status">Nothing to acknowledge: no first LinkedIn inbox poll fell short.</p>
    ) : null
  }
  return (
    <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3">
      <p className="font-medium">LinkedIn replies to check by hand</p>
      <ul className="mt-1 space-y-1">
        {row.warnings.map((warning) => (
          <li key={warning}>{renderInlineMarkdown(warning)}</li>
        ))}
      </ul>
      <p className="mt-1 text-muted-foreground">
        Once you have checked the older LinkedIn replies yourself, acknowledge it here.
      </p>
      <Button
        size="sm"
        className="mt-2"
        disabled={acknowledge.isPending}
        onClick={() => acknowledge.mutate()}
      >
        Acknowledge
      </Button>
      {acknowledge.isError && <p role="alert">{message(acknowledge.error)}</p>}
    </div>
  )
}

export function StatusBadge({ status }: { status: string }) {
  return (
    <span
      className={cn(
        'inline-flex h-5 shrink-0 items-center rounded-4xl px-2 text-xs font-medium',
        STATUS_CLASSES[status] ?? 'bg-muted text-muted-foreground',
      )}
    >
      {status}
    </span>
  )
}

function noteCount(count: number): string {
  return count === 1 ? '1 note' : `${count} notes`
}

/**
 * One row's text. Collapsed, it is the row's `summary` and a count of its
 * notes; expanded, its full `value` and the notes themselves. Warnings show
 * either way (#340): they are what makes the verdict "NOT clear".
 */
export function ProtectionDetail({ row, expanded }: { row: Protection; expanded: boolean }) {
  return (
    <>
      <p>
        {renderInlineMarkdown(expanded ? row.value : row.summary)}
        {!expanded && row.notes.length > 0 && (
          <span className="text-amber-800 dark:text-amber-300">
            {` (${noteCount(row.notes.length)})`}
          </span>
        )}
      </p>
      {row.warnings.length > 0 && (
        <ul className="mt-1 space-y-1 text-destructive">
          {row.warnings.map((warning) => (
            <li key={warning}>{renderInlineMarkdown(warning)}</li>
          ))}
        </ul>
      )}
      {/* Notes describe a choice, not a fault (#318): shown, never counted against the verdict. */}
      {expanded && row.notes.length > 0 && (
        <ul className="mt-1 space-y-1 text-amber-800 dark:text-amber-300" aria-label="Notes">
          {row.notes.map((note) => (
            <li key={note}>{renderInlineMarkdown(note)}</li>
          ))}
        </ul>
      )}
    </>
  )
}

interface RowProps {
  row: Protection
  expanded: boolean
  onAction: (action: RowAction) => void
}

function ProtectionRow({ row, expanded, onAction }: RowProps) {
  return (
    <tr className="border-t border-border/60 align-top">
      <th scope="row" className="py-2 pr-3 font-normal break-words">
        {row.name}
      </th>
      <td className="py-2 pr-3">
        <StatusBadge status={row.status} />
      </td>
      <td className="py-2 break-words">
        <ProtectionDetail row={row} expanded={expanded} />
        <RowActions row={row} onAction={onAction} />
      </td>
    </tr>
  )
}

function ProtectionBlock({ row, expanded, onAction }: RowProps) {
  return (
    <li className="rounded-lg border border-border/60 p-2">
      {/* `flex-wrap`, and the name keeps its own min-content width: when the
          name and the badge do not fit on one line, the badge moves below the
          name instead of the name breaking mid-word (#181 measured "linkedin
          session" split as "linkedi/n" at 390px). `break-words` on the name
          now only applies to a single word wider than the whole block. */}
      <div className="flex flex-wrap items-center justify-between gap-x-2 gap-y-1">
        <span className="max-w-full font-medium break-words">{row.name}</span>
        <StatusBadge status={row.status} />
      </div>
      <div className="mt-2 break-words text-muted-foreground">
        <ProtectionDetail row={row} expanded={expanded} />
      </div>
      <RowActions row={row} onAction={onAction} />
    </li>
  )
}

/** A button a posture row offers (#583), and the row it was clicked on. */
interface RowAction {
  action: string
  row: Protection
}

const ACTION_LABELS: Record<string, string> = {
  [RESET_BREAKER_ACTION]: 'Reset breaker',
  [FORGET_INBOX_OWNER_ACTION]: 'Forget inbox owner',
}

/**
 * The buttons a row offers right now (#583). The backend decides which apply (a
 * row's `actions`): **Reset breaker** only on a tripped breaker, **Forget inbox owner**
 * only where an owner mismatch shows that forgetting can fix. Each opens a confirmation.
 */
function RowActions({ row, onAction }: { row: Protection; onAction: (a: RowAction) => void }) {
  const actions = row.actions.filter((action) => action in ACTION_LABELS)
  if (actions.length === 0) return null
  return (
    <div className="mt-2 flex flex-wrap gap-2">
      {actions.map((action) => (
        <Button key={action} size="sm" variant="outline" onClick={() => onAction({ action, row })}>
          {ACTION_LABELS[action]}
        </Button>
      ))}
    </div>
  )
}

/** What each breaker stops while it is tripped, for the reset's confirmation (#583). */
const BREAKER_GUARDS: Record<string, string> = {
  route_changed_breaker:
    'It stops scheduled connections syncs from loading a connections page LinkedIn has changed or walled off, again at every interval.',
  answer_lost_limit:
    'It stops scheduled connections syncs from spending page views while LinkedIn’s answers keep arriving unreadable.',
  contact_info_breaker:
    'It stops scheduled enrichment runs while Contact info answers keep arriving unreadable.',
  inbox_breaker:
    'It stops scheduled LinkedIn inbox polls while the messaging page has changed or can’t be read.',
  inbox_owner_breaker:
    'It stops scheduled LinkedIn inbox polls while the page shows another LinkedIn mailbox than this account’s.',
}

/**
 * The one confirmation for whichever row button was clicked (#583). The rows render
 * twice (a table from `sm` up, stacked blocks below it), so the dialog lives here once
 * rather than in each row.
 */
function RowActionDialog({
  acting,
  rows,
  onClose,
}: {
  acting: RowAction | null
  rows: readonly Protection[]
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const done = async () => {
    await queryClient.invalidateQueries({ queryKey: postureQuery.queryKey })
    onClose()
  }
  // Every row offering a reset: the reset clears them all, so the person is shown them
  // all, and the backend checks that this is still the set that is tripped.
  const tripped = rows.filter((r) => r.actions.includes(RESET_BREAKER_ACTION))
  const reset = useMutation({
    mutationFn: () => resetBreaker(tripped.map((r) => r.key ?? '')),
    onSuccess: done,
  })
  const forget = useMutation({ mutationFn: forgetInboxOwner, onSuccess: done })
  const close = (open: boolean) => {
    if (open) return
    reset.reset()
    forget.reset()
    onClose()
  }

  if (acting?.action === FORGET_INBOX_OWNER_ACTION) {
    return (
      <ConfirmDialog
        open
        onOpenChange={close}
        title="Forget the recorded LinkedIn mailbox?"
        confirmLabel="Forget inbox owner"
        pending={forget.isPending}
        error={forget.isError ? message(forget.error) : null}
        onConfirm={() => forget.mutateAsync()}
      >
        <p>
          The LinkedIn inbox poll recorded which mailbox is this account’s, and the page has since
          shown another one, so polls stop rather than read it.
        </p>
        <p>
          If Chrome is signed in to another LinkedIn account, cancel and sign back in to yours
          instead. Forget the mailbox only if you moved netkeeper to a different LinkedIn account on
          purpose: the next inbox poll records whichever account the page shows.
        </p>
        <p>
          This touches no browser and visits nothing. Afterward, run an inbox poll by hand (
          <code className="font-mono text-xs">netkeeper linkedin inbox</code>) to record the new
          mailbox.
        </p>
      </ConfirmDialog>
    )
  }

  return (
    <ConfirmDialog
      open={acting?.action === RESET_BREAKER_ACTION}
      onOpenChange={close}
      title={tripped.length > 1 ? 'Reset the LinkedIn breakers?' : 'Reset the LinkedIn breaker?'}
      confirmLabel="Reset breaker"
      confirmVariant="default"
      pending={reset.isPending}
      error={reset.isError ? message(reset.error) : null}
      onConfirm={() => reset.mutateAsync()}
    >
      {tripped.map((row) => (
        <div key={row.name} data-testid="reset-breaker-row">
          <p className="font-medium text-foreground">{row.name}</p>
          {row.key != null && BREAKER_GUARDS[row.key] !== undefined && (
            <p>{BREAKER_GUARDS[row.key]}</p>
          )}
          <p className="mt-1">Why it tripped:</p>
          <ul className="list-disc space-y-1 pl-5">
            {row.warnings.map((warning) => (
              <li key={warning}>{renderInlineMarkdown(warning)}</li>
            ))}
          </ul>
        </div>
      ))}
      <p>
        Resetting clears every LinkedIn breaker at once, so scheduled connections, enrichment, and
        inbox runs may start again when due. If the cause is still there, the breaker trips again
        after the next failed runs. To check first, run one by hand instead: a run that completes
        clears its breaker without a reset.
      </p>
    </ConfirmDialog>
  )
}
