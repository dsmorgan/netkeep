import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'

import { contactEnrollmentsQuery } from '@/features/campaigns/api'
import { EnrollmentStatusBadge } from '@/features/campaigns/badges'
import { formatWhen } from '@/features/campaigns/format'
import { RecordResponseButton } from '@/features/campaigns/record-response'
import { ErrorNote, LoadingNote } from '@/features/crm/controls'

/**
 * The contact's campaign enrollments, newest first, and **Record a response** for one
 * that came outside netkeeper (#512).
 */
export function ContactCampaigns({ contactId, name }: { contactId: number; name: string }) {
  const enrollments = useQuery(contactEnrollmentsQuery(contactId))
  if (enrollments.isPending) return <LoadingNote label="Loading the campaigns…" />
  if (enrollments.isError) {
    return <ErrorNote label="The campaigns are unavailable." error={enrollments.error} />
  }
  if (enrollments.data.length === 0) {
    return <p className="text-muted-foreground">Not in any campaign.</p>
  }
  return (
    <div className="grid gap-3">
      <ul className="grid gap-2">
        {enrollments.data.map((e) => (
          <li key={e.id} className="flex flex-wrap items-center gap-2">
            <Link
              to="/campaigns/$campaignId"
              params={{ campaignId: String(e.campaign_id) }}
              className="underline underline-offset-4"
            >
              {e.campaign_name}
            </Link>
            <EnrollmentStatusBadge status={e.status} />
            {e.replied_at !== null && (
              <span className="text-muted-foreground">replied {formatWhen(e.replied_at)}</span>
            )}
          </li>
        ))}
      </ul>
      <div>
        <RecordResponseButton
          contactId={contactId}
          contactName={name}
          complete
          targets={enrollments.data.map((e) => ({
            id: e.id,
            campaignName: e.campaign_name,
            status: e.status,
          }))}
        />
      </div>
    </div>
  )
}
