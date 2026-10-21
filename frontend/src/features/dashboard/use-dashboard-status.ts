import { useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query'
import { useCallback } from 'react'

import { attentionQuery } from '@/features/attention/api'
import { useServerEvent } from '@/features/events/use-server-event'
import { budgetQuery, heatQuery, scheduleQuery, statusQuery } from '@/features/linkedin/api'
import { mailboxKeys, mailboxListQuery } from '@/features/mailboxes/api'

import { lastRunQuery } from './api'
import { buildChips, buildProblems, type Chip, type Problem, type Read } from './status'

function read<T>(query: UseQueryResult<T>): Read<T> {
  if (query.isPending) return { state: 'pending' }
  if (query.isError) return { state: 'error' }
  return { state: 'ok', data: query.data }
}

/**
 * The reads the chips and Needs you share, and what they make of them. Each is the
 * query its own page uses, so the cache is one and the pages cannot disagree. The
 * mailbox list refreshes on the `mailbox.status` event the backend's poll publishes,
 * as the re-auth banner does, so a status change shows without a reload; it is the
 * list, never `/mailboxes/status`, so it still answers while the Keychain is locked.
 */
export function useDashboardStatus(): { chips: Chip[]; problems: Problem[] } {
  const queryClient = useQueryClient()
  const mailboxes = useQuery(mailboxListQuery)
  const status = useQuery(statusQuery)
  const lastRun = useQuery(lastRunQuery)
  const schedule = useQuery(scheduleQuery)
  const budget = useQuery(budgetQuery)
  const heat = useQuery(heatQuery)
  const attention = useQuery(attentionQuery)
  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: mailboxKeys.all })
  }, [queryClient])
  useServerEvent('mailbox.status', refresh)

  const reads = {
    mailboxes: read(mailboxes),
    status: read(status),
    lastRun: lastRun.data?.items[0],
    schedule: read(schedule),
    budget: read(budget),
    heat: read(heat),
    attention: attention.data,
  }
  const problems = buildProblems(reads)
  return { chips: buildChips(reads, problems), problems }
}
