import { useQuery } from '@tanstack/react-query'
import { ExternalLink, TriangleAlert } from 'lucide-react'

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { cn } from '@/lib/utils'

import { gmailDraftUrl, gmailLeftoverDraftsQuery, type LeftoverDraft } from './api'
import { leftoverNote, leftoverReason } from './leftover'

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function when(iso: string): string {
  const at = new Date(iso)
  return Number.isNaN(at.getTime())
    ? iso
    : at.toLocaleDateString(undefined, { dateStyle: 'medium' })
}

function who(row: LeftoverDraft): string {
  if (row.kind === 'test') return row.mailbox_email ?? 'Your mailbox'
  return row.contact_name || 'Unnamed contact'
}

function step(row: LeftoverDraft): string {
  return row.step_position === null
    ? row.campaign_name
    : `${row.campaign_name}, step ${row.step_position}`
}

function Row({ row }: { row: LeftoverDraft }) {
  const note = leftoverNote(row)
  return (
    <li
      className={cn(
        'space-y-1 rounded-lg border p-2',
        row.warning !== null ? 'border-warn/60' : 'border-border/60',
        row.low_priority && 'opacity-80',
      )}
    >
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
        <span className="font-medium break-words">{who(row)}</span>
        <a
          href={gmailDraftUrl(row.mailbox_email, row.rfc822_message_ids)}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1 text-xs underline underline-offset-4"
        >
          <ExternalLink className="size-3" aria-hidden />
          Open in Gmail
        </a>
      </div>
      <p className="text-muted-foreground break-words">
        {leftoverReason(row)} · {step(row)} · drafted {when(row.at)}
      </p>
      {note !== null &&
        (note.warn ? (
          <p
            role="note"
            aria-label="Warning"
            className="flex items-start gap-1.5 rounded-md bg-warn-soft px-2 py-1 text-warn-ink"
          >
            <TriangleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden />
            {note.text}
          </p>
        ) : (
          <p className="text-xs text-muted-foreground">{note.text}</p>
        ))}
    </li>
  )
}

/**
 * Drafts netkeep no longer wants (#543): discarded by a merge, left on an enrollment
 * that ended, test sends, and drafts still waiting that would be wrong to send. netkeep
 * deletes nothing in Gmail, so each row links to the draft there for you to delete by
 * hand. Flagged rows come first, drafts left after a reply last. It never shows a
 * subject or a body.
 */
export function LeftoverCard() {
  const leftovers = useQuery(gmailLeftoverDraftsQuery)

  return (
    <Card size="sm" id="leftover-drafts">
      <CardHeader>
        <CardTitle level={2}>Leftover drafts</CardTitle>
        <CardDescription>
          Drafts netkeep no longer needs. netkeep deletes nothing in Gmail, so open each one there
          and delete it yourself.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {leftovers.isPending && <p role="status">Loading…</p>}
        {leftovers.isError && <p role="alert">{message(leftovers.error)}</p>}
        {leftovers.isSuccess && leftovers.data.items.length === 0 && (
          <p className="text-muted-foreground">No leftover drafts.</p>
        )}
        {leftovers.isSuccess && leftovers.data.items.length > 0 && (
          <ul className="space-y-2" aria-label="Leftover drafts">
            {leftovers.data.items.map((row) => (
              <Row key={`${row.kind}-${row.message_id ?? row.test_send_id}`} row={row} />
            ))}
          </ul>
        )}
        {leftovers.isSuccess && leftovers.data.more && (
          <p className="text-xs text-muted-foreground">
            There are more. Delete some of these in Gmail to see the rest.
          </p>
        )}
      </CardContent>
    </Card>
  )
}
