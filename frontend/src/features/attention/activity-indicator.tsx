import { useQuery } from '@tanstack/react-query'
import { Circle, LoaderCircle, Pause, TriangleAlert } from 'lucide-react'

import { cn } from '@/lib/utils'

import { ACTIVITY_TEXT, type ActivityState } from './activity'
import { attentionQuery } from './api'

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
 * full sentence on hover, on the LinkedIn page's Scheduled runs card. The top bar says
 * it in its LinkedIn status chip (#751), which also holds the live region.
 */
export function ActivityIndicator() {
  const attention = useQuery(attentionQuery)
  const activity = attention.data?.activity
  if (activity === undefined) return null
  return (
    <span
      data-testid="activity-indicator"
      data-state={activity.state}
      title={activity.label || undefined}
      className="flex min-w-0 items-center gap-1.5 text-sm text-muted-foreground"
    >
      <ActivityIcon state={activity.state} />
      <span className="truncate">{ACTIVITY_TEXT[activity.state]}</span>
      {activity.label !== '' && <span className="sr-only">. {activity.label}</span>}
    </span>
  )
}
