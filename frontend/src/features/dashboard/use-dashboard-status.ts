import { useQuery } from '@tanstack/react-query'

import { ATTENTION_EVERY_MS, attentionQuery } from '@/features/attention/api'
import { heatQuery, statusQuery } from '@/features/linkedin/api'
import { mailboxListQuery } from '@/features/mailboxes/api'
import { read } from '@/features/system-status/roll-up'

import { buildProblems, type Problem } from './status'

/**
 * The problems under Needs you. Each read is the query its own page uses, so the cache
 * is one and they cannot disagree. The top bar refreshes the mailbox list on the
 * `mailbox.status` event and the LinkedIn reads on each run's start and end
 * (`SystemStatusChips`); it is on every page, so this needn't. The session flag and
 * heat also change outside a run (`netkeep preflight`, the CLI) with no event, so they
 * refetch as often as `/attention` does.
 */
export function useDashboardStatus(): { problems: Problem[] } {
  const mailboxes = useQuery(mailboxListQuery)
  const status = useQuery({ ...statusQuery, refetchInterval: ATTENTION_EVERY_MS })
  const heat = useQuery({ ...heatQuery, refetchInterval: ATTENTION_EVERY_MS })
  const attention = useQuery(attentionQuery)
  const problems = buildProblems({
    mailboxes: read(mailboxes),
    status: read(status),
    heat: read(heat),
    attention: attention.data,
  })
  return { problems }
}
