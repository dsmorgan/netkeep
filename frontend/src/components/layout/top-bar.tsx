import { useQuery } from '@tanstack/react-query'
import { useRouterState } from '@tanstack/react-router'
import { Menu } from 'lucide-react'
import type { RefObject } from 'react'

import { Button } from '@/components/ui/button'
import { ActivityIndicator } from '@/features/attention/activity-indicator'
import { attentionQuery, menuLabel, needsYou } from '@/features/attention/api'
import { PollStatusHeader } from '@/features/poll-status/poll-status-header'

import { BackendHealth } from './backend-health'
import { sectionLabel } from './nav'
import { AttentionDot } from './nav-badge'

interface TopBarProps {
  menuButtonRef: RefObject<HTMLButtonElement | null>
  navId: string
  navOpen: boolean
  onToggleNav: () => void
}

export function TopBar({ menuButtonRef, navId, navOpen, onToggleNav }: TopBarProps) {
  const pathname = useRouterState({ select: (state) => state.location.pathname })
  const attention = useQuery(attentionQuery)
  // At phone width the badges are inside the closed drawer; the menu button stands in.
  const marked = needsYou(attention.data)
  return (
    <header className="flex h-11 items-center justify-between gap-2 border-b border-border/70 px-(--space-page)">
      <div className="flex min-w-0 items-center gap-2">
        {/* Below `sm` only: from `sm` up the sidebar is always in place. */}
        <Button
          ref={menuButtonRef}
          variant="ghost"
          size="icon-sm"
          className="relative -ml-2 sm:hidden"
          aria-label={menuLabel(attention.data)}
          aria-expanded={navOpen}
          aria-controls={navId}
          onClick={onToggleNav}
        >
          <Menu aria-hidden="true" />
          {marked && <AttentionDot className="absolute top-1 right-1 ring-2 ring-background" />}
        </Button>
        <h1 className="truncate font-heading text-page-title font-extrabold tracking-heading">
          {sectionLabel(pathname)}
        </h1>
      </div>
      <div className="flex min-w-0 items-center gap-3">
        <ActivityIndicator compact announce campaigns />
        <PollStatusHeader />
        <BackendHealth />
      </div>
    </header>
  )
}
