import { badgeText } from '@/features/attention/api'
import { cn } from '@/lib/utils'

/** The amber dot: no number, a state that blocks something until you deal with it. */
export function AttentionDot({ className }: { className?: string }) {
  return (
    <span
      aria-hidden="true"
      data-testid="attention-dot"
      className={cn('inline-block size-2 shrink-0 rounded-full bg-warn', className)}
    />
  )
}

/**
 * A nav item's badges: a filled pill with the count (capped at "99+") when items need
 * you, then an amber dot when a state blocks something. Pill and dot differ in shape,
 * not only in color. Hidden from assistive technology: {@link navStateText} names them.
 */
export function NavBadge({ count, flag }: { count: number; flag: boolean }) {
  if (count <= 0 && !flag) return null
  return (
    <span aria-hidden="true" className="ml-auto flex items-center gap-1.5">
      {count > 0 && (
        <span
          data-testid="attention-count"
          className="min-w-5 rounded-full bg-sidebar-primary px-1.5 text-center text-xs leading-5 font-bold text-sidebar-primary-foreground tabular-nums"
        >
          {badgeText(count)}
        </span>
      )}
      {flag && <AttentionDot />}
    </span>
  )
}
