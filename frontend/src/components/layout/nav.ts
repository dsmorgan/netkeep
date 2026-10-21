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
 * Primary navigation, in groups (#739): the Dashboard on its own, then People,
 * Outreach, Sources and System. Each group's links are in the order the person works
 * through them.
 *
 * `count` names the item's number badge in `GET /attention` and `flag` its amber dot
 * (#508); each sits on the page that lists the item. `countTo` is where the badge
 * sends you while it shows, when that is not the item's own page: draft imports are
 * listed on the import history, not the upload page.
 */
export const NAV_GROUPS = [
  {
    label: null,
    items: [
      { to: '/', label: 'Dashboard', icon: LayoutDashboard, count: 'dashboard', flag: 'dashboard' },
    ],
  },
  {
    label: 'People',
    items: [
      { to: '/contacts', label: 'Contacts', icon: Users },
      { to: '/triage', label: 'Triage', icon: ListChecks, count: 'triage' },
      { to: '/lists', label: 'Lists', icon: List },
    ],
  },
  {
    label: 'Outreach',
    items: [
      { to: '/campaigns', label: 'Campaigns', icon: Send },
      { to: '/templates', label: 'Templates', icon: FileText },
      { to: '/inbox', label: 'Inbox', icon: Inbox, count: 'inbox' },
    ],
  },
  {
    label: 'Sources',
    items: [
      { to: '/linkedin', label: 'LinkedIn', icon: Network, flag: 'linkedin' },
      { to: '/gmail', label: 'Gmail', icon: Mail, flag: 'gmail' },
      {
        to: '/imports',
        label: 'Imports',
        icon: Download,
        count: 'imports',
        countTo: '/imports/runs',
      },
      { to: '/exports', label: 'Exports', icon: Upload },
    ],
  },
  {
    label: 'System',
    items: [{ to: '/settings', label: 'Settings', icon: Settings, flag: 'settings' }],
  },
] as const satisfies ReadonlyArray<{
  /** The group's heading in the sidebar; null for the Dashboard, which stands alone. */
  label: string | null
  items: ReadonlyArray<{
    to: string
    label: string
    icon: LucideIcon
    count?: CountKey
    flag?: FlagKey
    countTo?: string
  }>
}>

type GroupItem = (typeof NAV_GROUPS)[number]['items'][number]

/** Every nav item, in sidebar order. */
export const NAV_ITEMS: readonly GroupItem[] = NAV_GROUPS.flatMap(
  (group): readonly GroupItem[] => group.items,
)

export type NavItem = (typeof NAV_ITEMS)[number]

/** Label of the nav item that owns `pathname`, for the top bar. */
export function sectionLabel(pathname: string): string {
  const item = NAV_ITEMS.find((candidate) =>
    candidate.to === '/' ? pathname === '/' : pathname.startsWith(candidate.to),
  )
  return item?.label ?? 'netkeep'
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
