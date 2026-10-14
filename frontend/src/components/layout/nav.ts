import {
  Download,
  FileText,
  Inbox,
  LayoutDashboard,
  List,
  ListChecks,
  Mail,
  Network,
  Send,
  Settings,
  Upload,
  Users,
  type LucideIcon,
} from 'lucide-react'

import type { CountKey, FlagKey } from '@/features/attention/api'

/**
 * Primary navigation, in the order of the pages table in spec section 14.3.
 *
 * `count` names the item's number badge in `GET /attention` and `flag` its amber dot
 * (#508); each sits on the page that lists the item. `countTo` is where the badge
 * sends you while it shows, when that is not the item's own page: draft imports are
 * listed on the import history, not the upload page.
 */
export const NAV_ITEMS = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard, count: 'dashboard', flag: 'dashboard' },
  { to: '/contacts', label: 'Contacts', icon: Users },
  { to: '/triage', label: 'Triage', icon: ListChecks, count: 'triage' },
  { to: '/lists', label: 'Lists', icon: List },
  { to: '/imports', label: 'Imports', icon: Download, count: 'imports', countTo: '/imports/runs' },
  { to: '/exports', label: 'Exports', icon: Upload },
  { to: '/linkedin', label: 'LinkedIn', icon: Network, flag: 'linkedin' },
  { to: '/gmail', label: 'Gmail', icon: Mail, flag: 'gmail' },
  { to: '/templates', label: 'Templates', icon: FileText },
  { to: '/campaigns', label: 'Campaigns', icon: Send },
  { to: '/inbox', label: 'Inbox', icon: Inbox, count: 'inbox' },
  { to: '/settings', label: 'Settings', icon: Settings, flag: 'settings' },
] as const satisfies ReadonlyArray<{
  to: string
  label: string
  icon: LucideIcon
  count?: CountKey
  flag?: FlagKey
  countTo?: string
}>

export type NavItem = (typeof NAV_ITEMS)[number]

/** Label of the nav item that owns `pathname`, for the top bar. */
export function sectionLabel(pathname: string): string {
  const item = NAV_ITEMS.find((candidate) =>
    candidate.to === '/' ? pathname === '/' : pathname.startsWith(candidate.to),
  )
  return item?.label ?? 'netkeeper'
}

/** How a count reads in its nav link's accessible name, with the exact number. */
const COUNT_TEXT: Record<CountKey, (n: number) => string> = {
  inbox: (n) => `${n} unhandled`,
  dashboard: (n) => `${n} need you`,
  triage: (n) => `${n} to triage`,
  imports: (n) => `${n} ${n === 1 ? 'draft' : 'drafts'} to review`,
}

/**
 * What a nav link adds to its name, for example ", 3 unhandled" or ". A mailbox
 * needs you to sign in again." (the flag is its own sentence). Empty when nothing shows. The badge itself is
 * `aria-hidden`, so this is the only place a screen reader hears it, and it carries
 * the exact count even when the pill reads "99+".
 */
export function navStateText(
  countKey: CountKey | undefined,
  count: number,
  flag: string | null,
): string {
  const counted = countKey !== undefined && count > 0 ? `, ${COUNT_TEXT[countKey](count)}` : ''
  return flag === null ? counted : `${counted}. ${flag}`
}
