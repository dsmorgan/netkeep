import { Link } from '@tanstack/react-router'
import { ArrowLeft } from 'lucide-react'
import type { ReactNode } from 'react'

import { buttonVariants } from '@/components/ui/button'
import { cn } from '@/lib/utils'

/**
 * A page that is not there: a short sentence and one link back, instead of a raw
 * backend message or a dead end (#584). `to` is a route path with no params.
 */
export function NotFound({
  children,
  to,
  backLabel,
}: {
  children: ReactNode
  to: '/' | '/contacts' | '/imports/runs'
  backLabel: string
}) {
  return (
    <div role="alert" className="grid justify-items-start gap-3">
      <p>{children}</p>
      <Link to={to} className={cn(buttonVariants({ variant: 'outline', size: 'sm' }))}>
        <ArrowLeft data-icon="inline-start" />
        {backLabel}
      </Link>
    </div>
  )
}
