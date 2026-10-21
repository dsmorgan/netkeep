import type { ReactNode } from 'react'

import { cn } from '@/lib/utils'

/**
 * A section a link can name with a hash, such as `/settings#gmail` (#739). A one-cell
 * grid, so the card inside still stretches to its row in a grid of cards; the scroll
 * margin keeps the section off the very top edge when a link scrolls to it.
 */
export function SectionAnchor({
  id,
  className,
  children,
}: {
  id: string
  className?: string
  children: ReactNode
}) {
  return (
    <div id={id} className={cn('grid scroll-mt-4', className)}>
      {children}
    </div>
  )
}
