import { useMutation, useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { useState, type ReactNode } from 'react'

import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { dashboardKeys } from '@/features/dashboard/api'
import { budgetQuery, linkedinKeys } from '@/features/linkedin/api'
import { ProfileViewNotice } from '@/features/linkedin/profile-view-notice'
import { RiskWarning } from '@/features/linkedin/risk-warning'
import { mailboxKeys } from '@/features/mailboxes/api'
import { pollStatusKeys, pollStatusQuery } from '@/features/poll-status/api'
import { systemStatusKeys } from '@/features/system-status/api'

import {
  automationKeys,
  automationQuery,
  doesMore,
  POSITION_LABEL,
  setGmailAutomation,
  setLinkedInAutomation,
  type Automation,
  type Depth,
  type Position,
} from './api'
import { JobList } from './job-list'

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * After a move: store the answer, and refetch what reads the same state (the LinkedIn
 * schedule and banner, the dashboard's upcoming runs, the poll status, the mailboxes'
 * arming, and the top bar's roll-up, whose pill words follow the position).
 */
function afterMove(queryClient: QueryClient, data: Automation): void {
  queryClient.setQueryData(automationKeys.all, data)
  for (const queryKey of [
    linkedinKeys.schedule(),
    linkedinKeys.status(),
    dashboardKeys.all,
    pollStatusKeys.all,
    mailboxKeys.all,
    systemStatusKeys.all,
  ]) {
    void queryClient.invalidateQueries({ queryKey })
  }
}

/** "Automatic: full upkeep", or the bare position when it isn't Automatic. */
function linkedinText(position: Position, depth: Depth): string {
  if (position !== 'automatic') return POSITION_LABEL[position]
  return `Automatic: ${depth === 'full' ? 'full upkeep' : 'quick checks'}`
}

/** What the scheduler starts on its own at each depth (spec 9.4, #829). */
const QUICK_JOBS = ['checks for new connections', 'LinkedIn inbox checks']
const FULL_JOBS = ['a weekly full sync', 'profile refreshes']

interface Target {
  position: Position
  depth: Depth
}

export interface LinkedInControl {
  /** The server's switch, once it answered. */
  state: Automation['linkedin'] | undefined
  loadError: string | null
  /** Move the switch; a move that does more opens the dialog first. */
  choose: (to: { position?: Position; depth?: Depth }) => void
  pending: boolean
  /** A refused move to the left, shown beside the switch (the dialog shows its own). */
  error: string | null
  /** The confirm dialog: render it outside any popover, so it outlives the popover. */
  dialog: ReactNode
}

/**
 * The LinkedIn switch's state and moves (#829). Moving left is one click; a move to
 * Automatic, or from quick checks to the weekly full sync, opens a dialog that lists
 * what will start on its own, with the budget's risk warning and the profile-view
 * notice when profile refreshes are part of it, and sends `confirm: true`.
 */
export function useLinkedInAutomation(): LinkedInControl {
  const queryClient = useQueryClient()
  const automation = useQuery(automationQuery)
  const [asking, setAsking] = useState<Target | null>(null)
  const budget = useQuery({ ...budgetQuery, enabled: asking !== null })
  const move = useMutation({
    mutationFn: setLinkedInAutomation,
    onSuccess: (data) => {
      afterMove(queryClient, data)
      setAsking(null)
    },
  })
  const state = automation.data?.linkedin

  const choose = ({ position, depth }: { position?: Position; depth?: Depth }) => {
    if (state === undefined || move.isPending) return
    const target = { position: position ?? state.position, depth: depth ?? state.depth }
    if (target.position === state.position && target.depth === state.depth) return
    move.reset()
    if (doesMore(state, target)) {
      setAsking(target)
      return
    }
    move.mutate({ position: target.position, depth, confirm: false })
  }

  const toAutomatic = asking !== null && state?.position !== 'automatic'
  const from = state === undefined ? '' : linkedinText(state.position, state.depth)
  const jobs =
    asking === null
      ? []
      : toAutomatic
        ? [...QUICK_JOBS, ...(asking.depth === 'full' ? FULL_JOBS : [])]
        : FULL_JOBS

  const dialog = (
    <ConfirmDialog
      open={asking !== null}
      onOpenChange={(open) => {
        if (open) return
        setAsking(null)
        move.reset()
      }}
      title={toAutomatic ? 'Let netkeep use LinkedIn on its own?' : 'Add the weekly full sync?'}
      confirmVariant="default"
      confirmLabel={toAutomatic ? 'Turn on Automatic' : 'Add the full sync'}
      pending={move.isPending}
      // The profile-visit notes come with the budget: confirm once they could show.
      confirmDisabled={asking?.depth === 'full' && budget.isPending}
      error={asking !== null && move.isError ? message(move.error) : null}
      onConfirm={() =>
        asking === null
          ? Promise.resolve()
          : move.mutateAsync({ position: asking.position, depth: asking.depth, confirm: true })
      }
    >
      {asking !== null && (
        <>
          <p className="text-foreground">
            You’re moving LinkedIn from <strong>{from}</strong> to{' '}
            <strong>{linkedinText(asking.position, asking.depth)}</strong>. From now on, within
            active hours, netkeep {toAutomatic ? 'starts' : 'also starts'} these without you
            watching:
          </p>
          <JobList items={jobs} />
          {asking.depth === 'full' && (
            <>
              <RiskWarning text={budget.data?.risk_warning} />
              <ProfileViewNotice text={budget.data?.profile_view_notice} />
            </>
          )}
          <p>
            It never sends a LinkedIn message on its own: that’s auto-send, a separate setting this
            switch doesn’t change. Moving back takes one click, any time.
          </p>
        </>
      )}
    </ConfirmDialog>
  )

  return {
    state,
    loadError: automation.isError ? message(automation.error) : null,
    choose,
    pending: move.isPending,
    error: asking === null && move.isError ? message(move.error) : null,
    dialog,
  }
}

export interface GmailControl {
  state: Automation['gmail'] | undefined
  loadError: string | null
  choose: (position: Position) => void
  pending: boolean
  error: string | null
  /** Why Automatic can't be chosen now; null when it can, or already is. */
  blockedReason: string | null
  /** "every 10 min", from the reply check's interval. */
  replyEvery: string
  dialog: ReactNode
}

/**
 * The Gmail switch's state and moves (#829), for every connected mailbox. Automatic
 * asks first, and can't be chosen while the server says it isn't ready (#510): the
 * stop is disabled and the server's reason shows.
 */
export function useGmailAutomation(): GmailControl {
  const queryClient = useQueryClient()
  const automation = useQuery(automationQuery)
  const poll = useQuery(pollStatusQuery)
  const [asking, setAsking] = useState(false)
  const move = useMutation({
    mutationFn: setGmailAutomation,
    onSuccess: (data) => {
      afterMove(queryClient, data)
      setAsking(false)
    },
  })
  const state = automation.data?.gmail
  const minutes = poll.data?.items.find((check) => check.key === 'gmail_replies')?.interval_minutes
  const replyEvery = minutes === undefined ? 'on a schedule' : `every ${minutes} min`
  const blockedReason =
    state === undefined || state.position === 'automatic' || state.automatic_ready
      ? null
      : (state.blocked_reason ?? 'Automatic isn’t available yet.')

  const choose = (position: Position) => {
    if (state === undefined || move.isPending || position === state.position) return
    move.reset()
    if (doesMore(state, { position })) {
      if (blockedReason === null) setAsking(true)
      return
    }
    move.mutate({ position, confirm: false })
  }

  const dialog = (
    <ConfirmDialog
      open={asking}
      onOpenChange={(open) => {
        if (open) return
        setAsking(false)
        move.reset()
      }}
      title="Let netkeep send email on its own?"
      confirmVariant="default"
      confirmLabel="Turn on Automatic"
      pending={move.isPending}
      error={asking && move.isError ? message(move.error) : null}
      onConfirm={() => move.mutateAsync({ position: 'automatic', confirm: true })}
    >
      <p className="text-foreground">
        You’re moving Gmail from <strong>{POSITION_LABEL[state?.position ?? 'manual']}</strong> to{' '}
        <strong>Automatic</strong>. From now on, netkeep does these without you pressing Send:
      </p>
      <JobList
        items={[
          'sends approved campaign email within sending hours',
          `checks replies ${replyEvery}`,
        ]}
      />
      <p>Moving back to Manual or Off takes one click, any time.</p>
    </ConfirmDialog>
  )

  return {
    state,
    loadError: automation.isError ? message(automation.error) : null,
    choose,
    pending: move.isPending,
    error: !asking && move.isError ? message(move.error) : null,
    blockedReason,
    replyEvery,
    dialog,
  }
}
