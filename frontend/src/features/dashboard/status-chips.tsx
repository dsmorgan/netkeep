import { useMutation } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { CircleDot, CircleX } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { navigation, startAuthorization } from '@/features/mailboxes/api'
import { TONE_CHIP_CLASSES } from '@/features/system-status/tone-classes'
import { cn } from '@/lib/utils'

import { NEEDS_YOU_ID, type Problem } from './status'

/**
 * The header's one chip (#751): how many things need you, linking to Needs you. Red
 * while something stopped, the accent otherwise; shape as well as color.
 */
export function NeedsYouChip({ count, stopped }: { count: number; stopped: boolean }) {
  const Icon = stopped ? CircleX : CircleDot
  return (
    <Link
      to="/"
      hash={NEEDS_YOU_ID}
      data-testid="needs-you-chip"
      data-tone={stopped ? 'bad' : 'busy'}
      className={cn(
        'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-semibold whitespace-nowrap hover:border-muted-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring',
        TONE_CHIP_CLASSES[stopped ? 'bad' : 'busy'],
      )}
    >
      <Icon aria-hidden="true" className="size-3.5 shrink-0" />
      Needs you · {count}
    </Link>
  )
}

/**
 * The problems at the top of Needs you, most urgent first, each with its fix. A
 * mailbox Google signed out gets **Re-authorize** here as well, which goes straight
 * to Google's page, as the mailbox card it replaces did.
 */
export function ProblemList({ problems }: { problems: Problem[] }) {
  if (problems.length === 0) return null
  return (
    <ul aria-label="Problems" className="flex flex-col gap-2">
      {problems.map((problem) => (
        <li
          key={problem.key}
          data-problem={problem.key}
          className="flex flex-wrap items-start justify-between gap-2 rounded-control bg-bad-soft px-3 py-2"
        >
          <div className="min-w-0 flex-1 space-y-0.5">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="destructive">Stopped</Badge>
              <h3 className="font-heading text-sm font-bold">{problem.title}</h3>
            </div>
            <p className="text-sm text-muted-foreground">{problem.detail}</p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {problem.mailboxId !== undefined && <Reauthorize mailboxId={problem.mailboxId} />}
            <Button
              size="sm"
              variant="outline"
              render={<Link to={problem.to} hash={problem.hash} />}
            >
              {problem.cta}
            </Button>
          </div>
        </li>
      ))}
    </ul>
  )
}

function Reauthorize({ mailboxId }: { mailboxId: number }) {
  const reauthorize = useMutation({
    mutationFn: startAuthorization,
    onSuccess: (url) => navigation.assign(url),
  })
  return (
    <>
      <Button
        size="sm"
        onClick={() => reauthorize.mutate(mailboxId)}
        disabled={reauthorize.isPending}
      >
        {reauthorize.isPending ? 'Opening Google…' : 'Re-authorize'}
      </Button>
      {reauthorize.isError && (
        <p role="alert" className="w-full text-sm text-destructive">
          {reauthorize.error.message}
        </p>
      )}
    </>
  )
}
