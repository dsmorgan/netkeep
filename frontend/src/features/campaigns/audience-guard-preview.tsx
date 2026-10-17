/**
 * The builder's guard preview (#513): how many of the chosen audience each guard would
 * skip, before the campaign is saved. A follow-up built from a past campaign's "No
 * reply" list goes through the same guards as any other campaign (in another campaign,
 * contacted recently, do-not-contact and the rest), and this says how many each one
 * would leave out. It gates nothing: the guards decide again when you enroll, and at
 * every send.
 */
import { useQuery } from '@tanstack/react-query'

import { ErrorNote } from '@/features/crm/controls'
import { useDebounced } from '@/features/crm/use-debounced'

import { audiencePreviewQuery, type AudiencePreviewIn } from './api'
import { sourceBody, sourceProblem, type AudienceSource } from './audience'

export function AudienceGuardPreview({
  source,
  channel,
}: {
  source: AudienceSource
  /** The first step's channel, the one the guards check at enrollment; null until picked. */
  channel: AudiencePreviewIn['channel'] | null
}) {
  const ready = source.kind !== 'none' && sourceProblem(source) === null && channel !== null
  const body: AudiencePreviewIn = { ...sourceBody(source), channel: channel ?? 'email' }
  // Debounced as text: the body is a new object on every render, its text is not.
  const settled = JSON.parse(useDebounced(JSON.stringify(body))) as AudiencePreviewIn
  const preview = useQuery({ ...audiencePreviewQuery(settled), enabled: ready })

  if (source.kind === 'none' || sourceProblem(source) !== null) return null
  if (channel === null) {
    return (
      <p className="text-sm text-muted-foreground">
        Pick the first step&apos;s template to see how many contacts the guards would skip.
      </p>
    )
  }
  if (preview.isError) {
    return <ErrorNote label="The guard preview is unavailable." error={preview.error} />
  }
  if (preview.data === undefined) {
    return <p className="text-sm text-muted-foreground">Checking the guards…</p>
  }
  const data = preview.data
  return (
    <section aria-label="Guard preview" className="flex flex-col gap-1 text-sm">
      <p className="font-medium">
        Of {data.audience} {data.audience === 1 ? 'contact' : 'contacts'}, {data.will_start} would
        start.
      </p>
      {data.skipped.length > 0 ? (
        <ul className="flex flex-col gap-0.5" aria-label="Skipped by guard">
          {data.skipped.map((row) => (
            <li key={row.reason} data-reason={row.reason} className="text-muted-foreground">
              {row.count} skipped: {row.label}
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-muted-foreground">No guard would skip anyone.</p>
      )}
      <p className="text-xs text-muted-foreground">
        A preview: the guards decide again when you enroll the audience, and at every send.
      </p>
    </section>
  )
}
