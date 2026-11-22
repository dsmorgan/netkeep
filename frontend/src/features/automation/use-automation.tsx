import { useMutation, useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { useState, type ReactNode } from 'react'

import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { dashboardKeys } from '@/features/dashboard/api'
import { budgetQuery, linkedinKeys } from '@/features/linkedin/api'
import { ProfileViewNotice } from '@/features/linkedin/profile-view-notice'
import { RiskWarning } from '@/features/linkedin/risk-warning'
import { mailboxKeys } from '@/features/mailboxes/api'
import { pollStatusKeys, pollStatusQuery } from '@/features/poll-status/api'
import { configSettingsQuery, type ConfigSettings } from '@/features/settings/api'
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
import { DepthChoice } from './depth-choice'
import { JobList } from './job-list'
import { automaticUnavailable } from './lines'
import { FailureList } from './read-failure'
import { failures } from './read-failures'

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

/** The full sync and enrichment, with enrichment's daily budget when it is known. */
function fullJobs(perDay: number | undefined): string[] {
  return [
    'a weekly full sync',
    perDay === undefined ? 'profile refreshes' : `profile refreshes, up to ${perDay} a day`,
  ]
}

function configValue(config: ConfigSettings | undefined, key: string): unknown {
  return config?.fields.find((field) => field.key === key)?.value
}

/** " (08:30–21:30)", the active hours in force; empty while unknown. */
function hoursText(config: ConfigSettings | undefined): string {
  const window = configValue(config, 'linkedin.active_hours')
  return Array.isArray(window) && window.length === 2 ? ` (${window.join('–')})` : ''
}

/** What the dialog says about LinkedIn auto-send, which no position changes (ADR 0008). */
function autoSendText(config: ConfigSettings | undefined): string {
  const on = configValue(config, 'campaigns.linkedin_auto_send')
  if (on === false) {
    return 'It never sends a LinkedIn message on its own: that’s auto-send, a separate setting that stays off.'
  }
  if (on === true) {
    return 'LinkedIn messages are auto-send’s: a separate setting, on now, that this switch doesn’t change.'
  }
  return 'Sending LinkedIn messages is auto-send’s: a separate setting this switch doesn’t change.'
}

interface Target {
  position: Position
  /** The depth the person picked in the dialog; null follows the stored one. */
  depth: Depth | null
}

export interface LinkedInControl {
  /** The server's switch, once it answered. */
  state: Automation['linkedin'] | undefined
  loadError: string | null
  /** A later read failed: the switch shows its last answer, and this says so. */
  staleError: string | null
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
 *
 * Opening the dialog reads the switch again, and the dialog confirms only once that
 * read, the settings (active hours, auto-send) and, for profile refreshes, the budget
 * have answered: so it never lists less than will start. A failed read keeps it
 * disabled and says so. Turning on Automatic chooses How thorough in the same
 * dialog, so quick checks never pass through full upkeep on the way.
 */
export function useLinkedInAutomation(): LinkedInControl {
  const queryClient = useQueryClient()
  const automation = useQuery(automationQuery)
  const [asking, setAsking] = useState<Target | null>(null)
  const budget = useQuery({ ...budgetQuery, enabled: asking !== null })
  const config = useQuery({ ...configSettingsQuery, enabled: asking !== null })
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
      setAsking({ position: target.position, depth: depth ?? null })
      // The list is built from a fresh answer, never a stale one.
      void queryClient.invalidateQueries({ queryKey: automationKeys.all })
      return
    }
    move.mutate({ position: target.position, depth, confirm: false })
  }

  const toAutomatic = asking !== null && state?.position !== 'automatic'
  const depth = asking?.depth ?? state?.depth ?? 'full'
  const from = state === undefined ? '' : linkedinText(state.position, state.depth)
  const perDay = budget.data?.budgets.find((row) => row.action === 'profile_visits')?.day.limit
  const full = fullJobs(perDay)
  const jobs =
    asking === null ? [] : toAutomatic ? [...QUICK_JOBS, ...(depth === 'full' ? full : [])] : full
  const needsBudget = depth === 'full'
  // One rule for every read the dialog shows: pending or failed, Confirm waits.
  const failed =
    asking === null
      ? []
      : failures([
          { key: 'switch', what: 'the switch', query: automation, needed: true },
          {
            key: 'config',
            what: 'your active hours and auto-send setting',
            query: config,
            needed: true,
          },
          {
            key: 'budget',
            what: 'the profile-visit limit and notes',
            query: budget,
            needed: needsBudget,
          },
        ])
  // Fetching as well as pending: a reopened dialog waits for fresh answers, never a
  // cached one (Settings keeps the settings read cached).
  const waiting =
    automation.isFetching ||
    config.isPending ||
    config.isFetching ||
    (needsBudget && (budget.isPending || budget.isFetching))

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
      confirmDisabled={waiting || failed.length > 0}
      error={move.isError ? message(move.error) : null}
      onConfirm={() =>
        asking === null
          ? Promise.resolve()
          : move.mutateAsync({ position: asking.position, depth, confirm: true })
      }
    >
      {asking !== null && (
        <>
          <p className="text-foreground">
            You’re moving LinkedIn from <strong>{from}</strong> to{' '}
            <strong>{linkedinText(asking.position, depth)}</strong>. From now on, within active
            hours{hoursText(config.data)}, netkeep {toAutomatic ? 'starts' : 'also starts'} these
            without you watching:
          </p>
          {toAutomatic && (
            <DepthChoice
              compact
              value={depth}
              onChoose={(next) => setAsking({ ...asking, depth: next })}
              busy={move.isPending}
            />
          )}
          <JobList items={jobs} />
          <FailureList items={failed} />
          {depth === 'full' && (
            <>
              <RiskWarning text={budget.data?.risk_warning} />
              <ProfileViewNotice text={budget.data?.profile_view_notice} />
            </>
          )}
          <p>
            {autoSendText(config.data)}{' '}
            {toAutomatic
              ? 'Moving back to Manual or Off takes one click, any time.'
              : 'Going back to quick checks takes one click, any time.'}
          </p>
        </>
      )}
    </ConfirmDialog>
  )

  return {
    state,
    loadError: automation.isError && state === undefined ? message(automation.error) : null,
    staleError:
      automation.isError && state !== undefined
        ? `Couldn’t refresh this: ${message(automation.error)}. It shows the last answer.`
        : null,
    choose,
    pending: move.isPending,
    error: asking === null && move.isError ? message(move.error) : null,
    dialog,
  }
}

export interface GmailControl {
  state: Automation['gmail'] | undefined
  loadError: string | null
  /** A later read failed: the switch shows its last answer, and this says so. */
  staleError: string | null
  choose: (position: Position) => void
  pending: boolean
  error: string | null
  /**
   * Why Automatic can't be chosen now, as the one sentence every place shows
   * (`automaticUnavailable`); null when it can, or already is.
   */
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
      : automaticUnavailable(state.blocked_reason)

  const gmailFailed = !asking
    ? []
    : failures([{ key: 'switch', what: 'the switch', query: automation, needed: true }])
  const choose = (position: Position) => {
    if (state === undefined || move.isPending || position === state.position) return
    move.reset()
    if (doesMore(state, { position })) {
      if (blockedReason !== null) return
      setAsking(true)
      // Confirm only on a fresh answer: the mailbox may have stopped being ready.
      void queryClient.invalidateQueries({ queryKey: automationKeys.all })
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
      confirmDisabled={automation.isFetching || gmailFailed.length > 0 || blockedReason !== null}
      error={move.isError ? message(move.error) : blockedReason}
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
      <FailureList items={gmailFailed} />
      <p>Moving back to Manual or Off takes one click, any time.</p>
    </ConfirmDialog>
  )

  return {
    state,
    loadError: automation.isError && state === undefined ? message(automation.error) : null,
    staleError:
      automation.isError && state !== undefined
        ? `Couldn’t refresh this: ${message(automation.error)}. It shows the last answer.`
        : null,
    choose,
    pending: move.isPending,
    error: !asking && move.isError ? message(move.error) : null,
    blockedReason,
    replyEvery,
    dialog,
  }
}
