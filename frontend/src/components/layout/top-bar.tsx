import { useQuery } from '@tanstack/react-query'
import { useRouterState } from '@tanstack/react-router'
import { Menu } from 'lucide-react'
import { useEffect, useRef, type RefObject } from 'react'

import { Button } from '@/components/ui/button'
import { attentionQuery, menuLabel, needsYou } from '@/features/attention/api'
import { SystemStatusChips } from '@/features/system-status/system-status'

import { sectionLabel } from './nav'
import { AttentionDot } from './nav-badge'

interface TopBarProps {
  menuButtonRef: RefObject<HTMLButtonElement | null>
  navId: string
  navOpen: boolean
  onToggleNav: () => void
}

/**
 * Publishes the header's height as `--top-bar-height` on the root, which sets
 * `scroll-padding-top`, so a section a link scrolls to, or a control Tab moves focus
 * to, lands below the fixed header rather than under it. jsdom has no ResizeObserver.
 */
function useTopBarHeight(header: RefObject<HTMLElement | null>): void {
  useEffect(() => {
    const element = header.current
    if (element === null || typeof ResizeObserver !== 'function') return
    const root = document.documentElement
    const observer = new ResizeObserver(() => {
      root.style.setProperty('--top-bar-height', `${element.offsetHeight}px`)
    })
    observer.observe(element)
    return () => {
      observer.disconnect()
      root.style.removeProperty('--top-bar-height')
    }
  }, [header])
}

/**
 * The page title and the three status pills (#835): fixed at the top of the content
 * column while the page scrolls under it, on a glass pane so what passes beneath stays
 * blurred behind the title. At phone width the pills wrap onto a second row.
 */
export function TopBar({ menuButtonRef, navId, navOpen, onToggleNav }: TopBarProps) {
  const pathname = useRouterState({ select: (state) => state.location.pathname })
  const header = useRef<HTMLElement>(null)
  useTopBarHeight(header)
  const attention = useQuery(attentionQuery)
  // At phone width the badges are inside the closed drawer; the menu button stands in.
  const marked = needsYou(attention.data)
  return (
    <header
      ref={header}
      data-testid="top-bar"
      className="nk-glass sticky top-0 z-20 flex min-h-11 flex-wrap items-center justify-between gap-x-3 gap-y-1 rounded-none border-b border-border/70 px-(--space-page) py-1.5 shadow-none"
    >
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
      <SystemStatusChips />
    </header>
  )
}
