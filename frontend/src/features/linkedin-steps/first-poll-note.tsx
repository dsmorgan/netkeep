import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'

import { pollStatusQuery } from '@/features/poll-status/api'

import { CHECK_INBOX_LABEL, FIRST_POLL_COMMAND, FIRST_POLL_NOTE } from './prefill-copy'

/**
 * Before the first inbox poll has completed, LinkedIn prefills are held until a person
 * runs one by hand (#417, #433). Read from the poll status; nothing shows when it can't
 * be read, or once a poll has completed. The Check inbox now button lives on the
 * LinkedIn page, so everywhere else the name links there (#584).
 */
export function FirstPollNote({ buttonIsHere = false }: { buttonIsHere?: boolean }) {
  const status = useQuery(pollStatusQuery)
  const inbox = status.data?.items.find((check) => check.key === 'linkedin_inbox')
  if (inbox === undefined || inbox.last_at !== null) return null
  const [before = '', after = ''] = FIRST_POLL_NOTE.split(CHECK_INBOX_LABEL)
  const [head = '', tail = ''] = before.split(FIRST_POLL_COMMAND)
  return (
    <p role="note" className="rounded-md border border-warn/60 bg-warn-soft px-2 py-1">
      {head}
      <code className="rounded bg-muted px-1 font-mono text-xs">{FIRST_POLL_COMMAND}</code>
      {tail}
      {buttonIsHere ? (
        CHECK_INBOX_LABEL
      ) : (
        <Link to="/linkedin" className="font-medium text-accent-ink underline underline-offset-4">
          {CHECK_INBOX_LABEL}
        </Link>
      )}
      {after}
    </p>
  )
}
