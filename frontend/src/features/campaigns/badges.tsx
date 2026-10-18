import { cn } from '@/lib/utils'

import type { CampaignStatus, EnrollmentStatus } from './api'
import { CAMPAIGN_STATUS_LABELS, ENROLLMENT_STATUS_LABELS } from './format'

const PILL =
  'inline-flex h-5 shrink-0 items-center rounded-4xl px-2 text-xs font-medium whitespace-nowrap'

const CAMPAIGN_CLASSES: Record<CampaignStatus, string> = {
  draft: 'bg-muted text-muted-foreground',
  reviewing: 'bg-warn-soft text-warn-ink',
  active: 'bg-ok-soft text-ok-ink',
  paused: 'bg-accent-soft text-accent-ink',
  completed: 'bg-muted text-foreground',
  archived: 'bg-muted text-muted-foreground',
}

export function CampaignStatusBadge({ status }: { status: CampaignStatus }) {
  return (
    <span className={cn(PILL, CAMPAIGN_CLASSES[status])}>{CAMPAIGN_STATUS_LABELS[status]}</span>
  )
}

const ENROLLMENT_CLASSES: Partial<Record<EnrollmentStatus, string>> = {
  active: 'bg-ok-soft text-ok-ink',
  replied: 'bg-accent-soft text-accent-ink',
  bounced: 'bg-bad-soft text-destructive',
  opted_out: 'bg-bad-soft text-destructive',
}

export function EnrollmentStatusBadge({ status }: { status: EnrollmentStatus }) {
  return (
    <span className={cn(PILL, ENROLLMENT_CLASSES[status] ?? 'bg-muted text-muted-foreground')}>
      {ENROLLMENT_STATUS_LABELS[status]}
    </span>
  )
}
