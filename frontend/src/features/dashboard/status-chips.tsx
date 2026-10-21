import { useMutation } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { navigation, startAuthorization } from '@/features/mailboxes/api'
import { cn } from '@/lib/utils'

import type { Chip, ChipTone, Problem } from './status'

const CHIP_CLASSES: Record<ChipTone, string> = {
  ok: 'border-ok/40 bg-ok-soft text-ok-ink',
  warn: 'border-warn/40 bg-warn-soft text-warn-ink',
  bad: 'border-bad/40 bg-bad-soft text-bad-ink',
  accent: 'border-primary/40 bg-accent-soft text-accent-ink',
  none: 'border-border bg-card/60 text-foreground',
}

const DOT_CLASSES: Record<ChipTone, string> = {
  ok: 'bg-ok',
  warn: 'bg-warn',
  bad: 'bg-bad',
  accent: 'bg-primary motion-safe:animate-pulse',
  none: 'bg-muted-foreground/50',
}

/**
 * The status chips (#739): one pill per fact, each a link to the page or section that
 * fixes it. A list, so a screen reader hears how many there are.
 */
export function StatusChips({ chips }: { chips: Chip[] }) {
  return (
    <ul aria-label="Status" className="flex flex-wrap gap-1.5">
      {chips.map((chip) => (
        <li key={chip.key} data-chip={chip.key} data-tone={chip.tone}>
          <Link
            to={chip.to}
            hash={chip.hash}
            className={cn(
              'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-semibold whitespace-nowrap hover:border-muted-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring',
              CHIP_CLASSES[chip.tone],
            )}
          >
            <span
              aria-hidden="true"
              className={cn('size-2 rounded-full', DOT_CLASSES[chip.tone])}
            />
            {chip.label}
          </Link>
        </li>
      ))}
    </ul>
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
