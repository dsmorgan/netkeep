import { useMutation } from '@tanstack/react-query'
import { useId, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { ErrorNote, Note } from '@/features/imports/notes'

import { type HistoryImport, type HistoryScan, importHistory, scanGmail } from './api'

const KIND_LABELS: Record<string, string> = {
  reply: 'Replies',
  unsubscribe: 'Unsubscribe requests',
  auto: 'Automatic answers',
  bounce: 'Bounces',
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : 'Something went wrong.'
}

function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`
}

/**
 * The old mailing tool's history (#601): the same two steps as `netkeep history import`
 * and `history scan-gmail` (docs/history-import.md), each a preview first and then a
 * write. There is no undo, because the command line has none: running an import again
 * adds nothing twice.
 */
export function HistoryImportPage() {
  return (
    <div className="flex max-w-4xl flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle level={2}>History from your old mailing tool</CardTitle>
          <CardDescription>
            Optional. Before netkeep sends its first campaign, tell it whom your old tool already
            emailed and what they said back. Without this, someone the old tool emailed, even
            someone who asked to be left alone, passes every campaign check. Do both steps in order.
            Each starts as a preview that writes nothing.
          </CardDescription>
        </CardHeader>
      </Card>
      <ImportSection />
      <ScanSection />
    </div>
  )
}

function ImportSection() {
  const fileId = useId()
  const createId = useId()
  const [file, setFile] = useState<File | null>(null)
  const [createMissing, setCreateMissing] = useState(false)
  const run = useMutation({
    mutationFn: (apply: boolean) => {
      if (file === null) throw new Error('Choose the workbook first.')
      return importHistory(file, { apply, createMissing })
    },
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle level={2}>Step 1: import the workbook</CardTitle>
        <CardDescription>
          Export the old tool&apos;s report from Google Sheets as an .xlsx workbook, one tab per
          campaign. netkeep records who each campaign reached and when.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="space-y-1.5">
          <Label htmlFor={fileId}>Old tool workbook (.xlsx)</Label>
          <Input
            id={fileId}
            type="file"
            accept=".xlsx"
            disabled={run.isPending}
            onChange={(event) => {
              setFile(event.target.files?.[0] ?? null)
              run.reset()
            }}
          />
        </div>
        <div className="flex items-center gap-2">
          <Checkbox
            id={createId}
            checked={createMissing}
            disabled={run.isPending}
            onCheckedChange={(checked) => {
              setCreateMissing(checked === true)
              run.reset()
            }}
          />
          <Label htmlFor={createId}>Create a contact for each address no contact holds</Label>
        </div>
        <div className="flex gap-2">
          <Button
            type="button"
            variant="outline"
            disabled={file === null || run.isPending}
            onClick={() => run.mutate(false)}
          >
            Preview the import
          </Button>
          {run.data && !run.data.applied && run.data.campaigns.length > 0 && (
            <Button type="button" disabled={run.isPending} onClick={() => run.mutate(true)}>
              Apply the import
            </Button>
          )}
        </div>
        {run.isPending && <p role="status">Reading the workbook…</p>}
        {run.isError && <ErrorNote>{message(run.error)}</ErrorNote>}
        {run.data && <ImportResult report={run.data} />}
      </CardContent>
    </Card>
  )
}

function ImportResult({ report }: { report: HistoryImport }) {
  return (
    <div className="space-y-3">
      <p role="status" className="font-medium">
        {report.applied
          ? 'Imported. Running it again adds nothing twice.'
          : 'Preview only: nothing was written.'}
      </p>
      {report.campaigns.length === 0 && <Note>No campaign tabs found in this workbook.</Note>}
      {report.campaigns.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-left">
            <thead className="text-muted-foreground">
              <tr>
                {[
                  'Campaign',
                  'Started',
                  'Last batch',
                  'Listed',
                  'Matched',
                  'Unmatched',
                  'Ambiguous',
                  'Created',
                  'Clicks',
                  'Opens',
                  'Bounces',
                  'Sent to',
                  'Unlisted',
                ].map((heading) => (
                  <th key={heading} scope="col" className="py-1 pr-3 font-medium">
                    {heading}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {report.campaigns.map((c) => (
                <tr key={c.tab} className="border-t border-border/60">
                  <th scope="row" className="py-2 pr-3 font-normal">
                    {c.name}
                  </th>
                  <td className="py-2 pr-3">{c.started_on}</td>
                  <td className="py-2 pr-3">{c.last_batch_on ?? '-'}</td>
                  <td className="py-2 pr-3 tabular-nums">{c.listed}</td>
                  <td className="py-2 pr-3 tabular-nums">{c.matched}</td>
                  <td className="py-2 pr-3 tabular-nums">{c.unmatched}</td>
                  <td className="py-2 pr-3 tabular-nums">{c.ambiguous}</td>
                  <td className="py-2 pr-3 tabular-nums">{c.created}</td>
                  <td className="py-2 pr-3 tabular-nums">{c.clicks}</td>
                  <td className="py-2 pr-3 tabular-nums">{c.opens ?? '-'}</td>
                  <td className="py-2 pr-3 tabular-nums">{c.bounces ?? '-'}</td>
                  <td className="py-2 pr-3 tabular-nums">{c.sent_to ?? '-'}</td>
                  <td className="py-2 tabular-nums">{c.unlisted ?? '-'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {report.campaigns.flatMap((c) =>
        c.warnings.map((warning) => (
          <Note key={`${c.tab}-${warning}`}>
            {c.name}: {warning}
          </Note>
        )),
      )}
      <p>
        {plural(
          report.campaigns.reduce((n, c) => n + c.created, 0),
          'contact',
        )}{' '}
        created, {plural(report.new_campaigns, 'new campaign')},{' '}
        {plural(report.new_rows, 'new recipient row')},{' '}
        {plural(report.new_interactions, 'new timeline entry', 'new timeline entries')}.
      </p>
      {report.skipped.length > 0 && (
        <ErrorNote>
          {plural(report.skipped.length, 'tab')} skipped, and nothing from them was imported:{' '}
          {report.skipped.map((s) => `${s.tab} (${s.reason})`).join(', ')}
        </ErrorNote>
      )}
      <AddressList
        title="No contact holds these addresses"
        addresses={report.unmatched}
        hint="Check the box above to create a contact for each, then preview again."
      />
      <AddressList
        title="More than one contact holds these addresses"
        addresses={report.ambiguous}
        hint="Merge the duplicate contacts, then import again."
      />
      {report.unlisted > 0 && (
        <Note>
          The workbook names only the people who opened, clicked, or bounced. {report.unlisted}{' '}
          recipient(s) across these campaigns are not named in it, so netkeep cannot know the old
          tool emailed them. Check your first batch by hand.
        </Note>
      )}
    </div>
  )
}

function AddressList({
  title,
  addresses,
  hint,
}: {
  title: string
  addresses: readonly string[]
  hint: string
}) {
  if (addresses.length === 0) return null
  return (
    <div className="space-y-1">
      <p className="font-medium">
        {title} ({addresses.length})
      </p>
      <p className="text-muted-foreground">{hint}</p>
      <ul className="max-h-40 overflow-y-auto text-muted-foreground">
        {addresses.map((address) => (
          <li key={address}>{address}</li>
        ))}
      </ul>
    </div>
  )
}

function ScanSection() {
  const mailboxId = useId()
  const rescanId = useId()
  const limitId = useId()
  const [mailbox, setMailbox] = useState('')
  const [rescan, setRescan] = useState(false)
  const [limit, setLimit] = useState('')
  const parsedLimit = limit.trim() === '' ? null : Number(limit)
  const limitOk = parsedLimit === null || (Number.isInteger(parsedLimit) && parsedLimit >= 1)
  const run = useMutation({
    mutationFn: (apply: boolean) =>
      scanGmail({
        apply,
        rescan,
        limit: parsedLimit,
        mailbox: mailbox.trim() === '' ? null : mailbox.trim(),
      }),
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle level={2}>Step 2: scan Gmail for replies</CardTitle>
        <CardDescription>
          For each imported recipient, netkeep searches your Gmail for replies, unsubscribe
          requests, automatic answers, and bounces. It reads only headers and Gmail&apos;s short
          preview, and changes nothing in Gmail: no labels, no read state, no deletes. Connect Gmail
          first on the Gmail page.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="space-y-1.5">
          <Label htmlFor={mailboxId}>Mailbox (optional)</Label>
          <Input
            id={mailboxId}
            value={mailbox}
            placeholder="The connected mailbox"
            disabled={run.isPending}
            onChange={(event) => {
              setMailbox(event.target.value)
              run.reset()
            }}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor={limitId}>Try the first few recipients only (optional)</Label>
          <Input
            id={limitId}
            inputMode="numeric"
            value={limit}
            placeholder="All of them"
            disabled={run.isPending}
            onChange={(event) => {
              setLimit(event.target.value)
              run.reset()
            }}
          />
          <p className="text-muted-foreground">A limited run skips the search by subject.</p>
        </div>
        <div className="flex items-center gap-2">
          <Checkbox
            id={rescanId}
            checked={rescan}
            disabled={run.isPending}
            onCheckedChange={(checked) => {
              setRescan(checked === true)
              run.reset()
            }}
          />
          <Label htmlFor={rescanId}>Scan recipients an earlier scan already covered</Label>
        </div>
        {!limitOk && <ErrorNote>The limit is a whole number of at least 1.</ErrorNote>}
        <div className="flex gap-2">
          <Button
            type="button"
            variant="outline"
            disabled={!limitOk || run.isPending}
            onClick={() => run.mutate(false)}
          >
            Preview the scan
          </Button>
          {run.data && !run.data.applied && !run.data.nothing_to_scan && (
            <Button type="button" disabled={run.isPending} onClick={() => run.mutate(true)}>
              Apply the scan
            </Button>
          )}
        </div>
        {run.isPending && (
          <p role="status">
            Searching Gmail, read-only. This can take a few minutes for a long list; leave this page
            open.
          </p>
        )}
        {run.isError && <ErrorNote>{message(run.error)}</ErrorNote>}
        {run.data && <ScanResult report={run.data} />}
      </CardContent>
    </Card>
  )
}

function ScanResult({ report }: { report: HistoryScan }) {
  if (report.nothing_to_scan) {
    return (
      <Note>
        Nothing to scan: there are no imported recipients or campaign subjects left unscanned.
        Choose the rescan box to scan them again.
      </Note>
    )
  }
  return (
    <div className="space-y-3">
      <p role="status" className="font-medium">
        {report.applied ? 'Applied.' : 'Preview only: nothing was written.'} Scanned{' '}
        {plural(report.scanned, 'recipient')}; nothing found for {report.nothing_found}.
      </p>
      {report.stopped !== null && (
        <ErrorNote>
          Gmail stopped the scan ({report.stopped}); {plural(report.remaining, 'recipient')} not
          reached.
          {report.applied
            ? ' What it finished is saved. Run the scan again to continue.'
            : ' Apply runs the scan again and saves what it finishes.'}
        </ErrorNote>
      )}
      {report.campaigns.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-left">
            <thead className="text-muted-foreground">
              <tr>
                {[
                  'Campaign',
                  'Replies',
                  'Unsubscribes',
                  'Automatic',
                  'Bounces',
                  'By subject (review)',
                  'Of them unsubscribe',
                ].map((heading) => (
                  <th key={heading} scope="col" className="py-1 pr-3 font-medium">
                    {heading}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {report.campaigns.map((c) => (
                <tr key={c.campaign} className="border-t border-border/60">
                  <th scope="row" className="py-2 pr-3 font-normal">
                    {c.campaign}
                  </th>
                  <td className="py-2 pr-3 tabular-nums">{c.replies}</td>
                  <td className="py-2 pr-3 tabular-nums">{c.unsubscribes}</td>
                  <td className="py-2 pr-3 tabular-nums">{c.auto}</td>
                  <td className="py-2 pr-3 tabular-nums">{c.bounces}</td>
                  <td className="py-2 pr-3 tabular-nums">{c.by_subject}</td>
                  <td className="py-2 tabular-nums">{c.by_subject_unsubscribe}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {report.samples.map((sample) => (
        <AddressList
          key={sample.kind}
          title={KIND_LABELS[sample.kind] ?? sample.kind}
          addresses={sample.addresses}
          hint="The first few addresses."
        />
      ))}
      <AddressList
        title="Found by subject, not in the workbook"
        addresses={report.subject_samples}
        hint="For review only: flagged, never put on do-not-contact or do-not-send."
      />
      <p>
        {plural(report.flagged, 'contact')} flagged for review, {report.opted_out} opted out,{' '}
        {plural(report.new_interactions, 'new timeline entry', 'new timeline entries')}.
      </p>
      {report.matched_late > 0 && (
        <Note>
          {plural(report.matched_late, 'recipient')} unmatched at import now match a contact and got
          the imported email-out entry.
        </Note>
      )}
      {report.unconfirmed_notices > 0 && (
        <Note>
          {plural(report.unconfirmed_notices, 'failure notice')} skipped: they did not name the
          searched address exactly.
        </Note>
      )}
      {report.subject_search_skipped && (
        <Note>Subject search skipped: a limit is set. Run without it to search by subject.</Note>
      )}
      {report.subject_capped.map((name) => (
        <Note key={`capped-${name}`}>
          {name}: the subject search hit its limit, so some messages may be unread.
        </Note>
      ))}
      {report.subject_unsearchable.map((name) => (
        <Note key={`empty-${name}`}>
          {name}: its subject is empty once tags and reply prefixes are stripped, so it was not
          searched.
        </Note>
      ))}
      {report.no_subject.length > 0 && (
        <Note>
          {plural(report.no_subject.length, 'campaign')} have no subject, so no subject search:{' '}
          {report.no_subject.join(', ')}
        </Note>
      )}
      <AddressList
        title="Wrote back, but no single contact holds the address"
        addresses={report.no_contact}
        hint="Import again with the create-contacts box checked, or merge duplicates, then rescan."
      />
      {report.applied && report.flagged > 0 && (
        <Note>
          Next, open each flagged contact (filter the contacts table on Needs review), read the
          reply in the timeline, and confirm them or set Do not contact.
        </Note>
      )}
    </div>
  )
}
