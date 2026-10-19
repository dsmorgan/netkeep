import { useQuery } from '@tanstack/react-query'
import { Link, useRouterState } from '@tanstack/react-router'
import { X } from 'lucide-react'
import { useEffect, useRef } from 'react'

import { Button } from '@/components/ui/button'
import { attentionQuery, type Attention } from '@/features/attention/api'
import { cn } from '@/lib/utils'

import { NAV_ITEMS, navStateText, type NavItem } from './nav'
import { NavBadge } from './nav-badge'
import { ThemeSwitch } from './theme-switch'

interface SidebarProps {
  id: string
  /** Below `sm` only: whether the off-canvas sidebar is showing. From `sm` up it always is. */
  open: boolean
  /** Close the off-canvas sidebar, returning focus to the menu button when `returnFocus`. */
  onClose: (returnFocus: boolean) => void
}

/**
 * The primary navigation. From `sm` up it is a fixed column beside the page;
 * below `sm` it slides in over the page while `open`, and is `invisible` (out
 * of the tab order and the accessibility tree) while closed, so a keyboard
 * never lands on a link nobody can see.
 *
 * Opening it moves focus to its first link; Escape and the close button
 * close it and put focus back on the menu button that opened it. Following a
 * link closes it without moving focus, since the router's navigation does.
 *
 * Open, it is a modal dialog (#364 S2): `role="dialog"`, `aria-modal`, and Tab
 * and Shift+Tab wrap around inside it. `AppShell` makes everything else inert.
 */
export function Sidebar({ id, open, onClose }: SidebarProps) {
  const nav = useRef<HTMLElement>(null)
  const drawer = useRef<HTMLElement>(null)
  const attention = useQuery(attentionQuery)
  const pathname = useRouterState({ select: (state) => state.location.pathname })

  useEffect(() => {
    if (!open) return
    nav.current?.querySelector<HTMLElement>('a')?.focus()
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') {
        onClose(true)
        return
      }
      if (event.key !== 'Tab' || drawer.current === null) return
      const focusable = drawer.current.querySelectorAll<HTMLElement>(
        'a[href], button, input:checked',
      )
      const first = focusable.item(0)
      const last = focusable.item(focusable.length - 1)
      if (focusable.length === 0) return
      const active = document.activeElement
      const inside = active instanceof Node && drawer.current.contains(active)
      if (event.shiftKey && (active === first || !inside)) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && (active === last || !inside)) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [open, onClose])

  return (
    <aside
      ref={drawer}
      id={id}
      data-open={open}
      {...(open ? { role: 'dialog', 'aria-modal': true, 'aria-label': 'Navigation' } : {})}
      className={cn(
        // One screen tall with its own scroll, from `sm` up as in the drawer, so the theme
        // footer stays in view and never covers a link (#666). The nav scrolls between
        // the header and the footer; the aside scrolls only if those two alone do not fit.
        'relative z-[1] flex w-48 shrink-0 flex-col overflow-y-auto border-r border-sidebar-border bg-sidebar text-sidebar-foreground sm:sticky sm:top-0 sm:h-screen',
        'max-sm:fixed max-sm:inset-y-0 max-sm:left-0 max-sm:z-40 max-sm:w-64 max-sm:shadow-lg max-sm:transition-transform',
        open ? 'max-sm:translate-x-0' : 'max-sm:invisible max-sm:-translate-x-full',
      )}
    >
      <div className="flex h-11 shrink-0 items-center justify-between border-b border-sidebar-border px-3 font-heading text-base font-extrabold tracking-heading text-sidebar-accent-foreground">
        <span className="flex items-center gap-2">
          <span
            aria-hidden="true"
            className="size-2.5 rounded-[3px] bg-sidebar-primary shadow-[0_0_0_3px_color-mix(in_oklab,var(--sidebar-primary)_30%,transparent)]"
          />
          netkeeper
        </span>
        <Button
          variant="ghost"
          size="icon-sm"
          className="text-sidebar-foreground hover:bg-sidebar-accent hover:text-sidebar-accent-foreground sm:hidden"
          aria-label="Close navigation"
          onClick={() => onClose(true)}
        >
          <X aria-hidden="true" />
        </Button>
      </div>
      <nav
        ref={nav}
        aria-label="Primary"
        className="flex min-h-0 flex-1 flex-col gap-px overflow-y-auto p-2"
      >
        {NAV_ITEMS.map((item) => (
          <NavLink
            key={item.to}
            item={item}
            attention={attention.data}
            pathname={pathname}
            onFollow={() => onClose(false)}
          />
        ))}
      </nav>
      <div data-testid="sidebar-footer" className="shrink-0 border-t border-sidebar-border p-3">
        <ThemeSwitch />
      </div>
    </aside>
  )
}

const LINK_CLASS =
  'flex items-center gap-2 rounded-md px-2 py-1.5 text-sidebar-foreground hover:bg-sidebar-accent/70 hover:text-sidebar-accent-foreground focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-sidebar-ring data-[status=active]:bg-sidebar-accent data-[status=active]:font-semibold data-[status=active]:text-sidebar-accent-foreground'

function owns(item: NavItem, pathname: string): boolean {
  return item.to === '/' ? pathname === '/' : pathname.startsWith(item.to)
}

/**
 * One nav link with its badges (#508). Its accessible name carries their state
 * ("Inbox, 3 unhandled"). While a badge with a `countTo` shows, the link goes there,
 * and it still reads as current anywhere in its section.
 */
function NavLink({
  item,
  attention,
  pathname,
  onFollow,
}: {
  item: NavItem
  attention: Attention | undefined
  pathname: string
  onFollow: () => void
}) {
  const countKey = 'count' in item ? item.count : undefined
  const flagKey = 'flag' in item ? item.flag : undefined
  const count = countKey === undefined || attention === undefined ? 0 : attention.counts[countKey]
  const flag = flagKey === undefined || attention === undefined ? null : attention.flags[flagKey]
  const elsewhere = 'countTo' in item && count > 0 ? item.countTo : undefined
  return (
    <Link
      to={elsewhere ?? item.to}
      activeOptions={{ exact: item.to === '/' }}
      onClick={onFollow}
      className={LINK_CLASS}
      {...(elsewhere !== undefined && owns(item, pathname)
        ? { 'data-status': 'active', 'aria-current': 'page' as const }
        : {})}
    >
      <item.icon className="size-4" aria-hidden="true" />
      {item.label}
      <span className="sr-only">{navStateText(countKey, count, flag)}</span>
      <NavBadge count={count} flag={flag !== null} />
    </Link>
  )
}
