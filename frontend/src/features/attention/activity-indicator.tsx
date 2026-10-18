import { useQuery } from '@tanstack/react-query'
import { Circle, LoaderCircle, Pause, TriangleAlert } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

import { cn } from '@/lib/utils'

import { ACTIVITY_TEXT, CAMPAIGN_SENDING_TEXT, announcement, type ActivityState } from './activity'
import { attentionQuery } from './api'
import { useCampaignSending } from './use-campaign-sending'

const ICON_CLASS = 'size-4 shrink-0'

/** Each state's icon, distinct in shape and not only in color. */
function ActivityIcon({ state }: { state: ActivityState }) {
  switch (state) {
    case 'running':
      // Spins, unless the person asked for reduced motion.
      return (
        <LoaderCircle
          aria-hidden="true"
          data-icon="running"
          className={cn(ICON_CLASS, 'animate-spin text-accent-ink motion-reduce:animate-none')}
        />
      )
    case 'stopped':
      return (
        <TriangleAlert
          aria-hidden="true"
          data-icon="stopped"
          className={cn(ICON_CLASS, 'text-warn')}
        />
      )
    case 'paused':
      return <Pause aria-hidden="true" data-icon="paused" className={ICON_CLASS} />
    case 'armed':
      return (
        <Circle
          aria-hidden="true"
          data-icon="armed"
          className={cn(ICON_CLASS, 'fill-current text-accent-ink')}
        />
      )
    case 'idle':
      return (
        <Circle
          aria-hidden="true"
          data-icon="idle"
          className={cn(ICON_CLASS, 'text-muted-foreground')}
        />
      )
  }
}

/**
 * What LinkedIn is doing (#347), from `GET /attention`: an icon and a short text, the
 * full sentence on hover. A campaign send batch shows beside it as its own "Campaigns:
 * sending" (#547) and never changes the LinkedIn state; the Gmail poll does not show.
 * `compact` (the top bar) keeps only the icon below `sm`, with the text still there for
 * screen readers.
 *
 * `announce` mounts the one polite live region: it speaks only when the state
 * changes, never on the first answer or on a refetch that changes nothing. Only the
 * top bar's indicator announces, so a page that shows a second one never speaks twice.
 */
export function ActivityIndicator({
  compact = false,
  announce = false,
  campaigns = false,
}: {
  compact?: boolean
  announce?: boolean
  /** Also show a campaign send batch (the top bar does; the LinkedIn schedule card does not). */
  campaigns?: boolean
}) {
  const attention = useQuery(attentionQuery)
  const campaignSending = useCampaignSending()
  const activity = attention.data?.activity
  const [spoken, setSpoken] = useState('')
  const previous = useRef<ActivityState | null>(null)

  useEffect(() => {
    if (activity === undefined) return
    if (previous.current !== null && previous.current !== activity.state) {
      setSpoken(announcement(activity))
    }
    previous.current = activity.state
  }, [activity])

  return (
    <>
      {activity !== undefined && (
        <span
          data-testid="activity-indicator"
          data-state={activity.state}
          title={activity.label || undefined}
          className="flex min-w-0 items-center gap-1.5 text-sm text-muted-foreground"
        >
          <ActivityIcon state={activity.state} />
          <span className={cn('truncate', compact && 'max-sm:sr-only')}>
            {ACTIVITY_TEXT[activity.state]}
          </span>
          {activity.label !== '' && <span className="sr-only">. {activity.label}</span>}
        </span>
      )}
      {campaigns && campaignSending && (
        <span
          data-testid="campaign-activity"
          className="flex min-w-0 items-center gap-1.5 text-sm text-muted-foreground"
        >
          <LoaderCircle
            aria-hidden="true"
            data-icon="campaign-sending"
            className={cn(ICON_CLASS, 'animate-spin text-accent-ink motion-reduce:animate-none')}
          />
          <span className={cn('truncate', compact && 'max-sm:sr-only')}>
            {CAMPAIGN_SENDING_TEXT}
          </span>
        </span>
      )}
      {announce && (
        <span role="status" aria-live="polite" className="sr-only" data-testid="activity-live">
          {spoken}
        </span>
      )}
    </>
  )
}
