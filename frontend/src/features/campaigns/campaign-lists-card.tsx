/**
 * "Make a list from this campaign" (#513): one button per outcome, with its count, each
 * making a list of those people to start a follow-up campaign from.
 *
 * A snapshot (a static list, the default) keeps who is in it now, so a follow-up's
 * audience does not shift as late replies arrive. A live list (a smart list) stores the
 * filter and follows the campaign. Either way it is an ordinary list: nothing is sent,
 * and a campaign built from it goes through every guard.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Callout, ErrorNote, LoadingNote } from '@/features/crm/controls'
import { listsQuery } from '@/features/crm/api'

import {
  campaignKeys,
  createListFromCampaign,
  errorText,
  outcomesQuery,
  type OutcomeCount,
  type OutcomeList,
} from './api'

type Kind = 'static' | 'smart'

function capitalized(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1)
}

export function CampaignListsCard({ campaignId }: { campaignId: number }) {
  const queryClient = useQueryClient()
  const outcomes = useQuery(outcomesQuery(campaignId))
  const [kind, setKind] = useState<Kind>('static')
  const [made, setMade] = useState<OutcomeList | null>(null)

  const make = useMutation({
    mutationFn: (row: OutcomeCount) =>
      createListFromCampaign(campaignId, { outcome: row.outcome, kind }),
    onSuccess: async (list) => {
      setMade(list)
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: listsQuery.queryKey }),
        queryClient.invalidateQueries({ queryKey: campaignKeys.outcomes(campaignId) }),
      ])
    },
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle level={2}>Make a list from this campaign</CardTitle>
        <CardDescription>
          One list per outcome, to start a follow-up campaign from. A campaign built from it still
          goes through every guard, so nobody is contacted twice by mistake.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 text-sm">
        <fieldset className="flex flex-col gap-1">
          <legend className="text-sm font-medium">The list is</legend>
          <label className="flex items-start gap-2">
            <input
              type="radio"
              name={`campaign-${campaignId}-list-kind`}
              checked={kind === 'static'}
              onChange={() => setKind('static')}
            />
            <span>
              A snapshot <span className="text-muted-foreground">(static)</span>: the people the
              outcome holds now. A late reply doesn&apos;t change it.
            </span>
          </label>
          <label className="flex items-start gap-2">
            <input
              type="radio"
              name={`campaign-${campaignId}-list-kind`}
              checked={kind === 'smart'}
              onChange={() => setKind('smart')}
            />
            <span>
              Live <span className="text-muted-foreground">(smart)</span>: re-evaluated whenever
              it&apos;s read, so someone who replies late leaves &ldquo;No reply&rdquo; on their
              own.
            </span>
          </label>
        </fieldset>

        {outcomes.isPending ? (
          <LoadingNote label="Counting the outcomes…" />
        ) : outcomes.isError ? (
          <ErrorNote label="The outcomes are unavailable." error={outcomes.error} />
        ) : (
          <div className="flex flex-wrap gap-2" role="group" aria-label="Outcomes">
            {outcomes.data.outcomes.map((row) => (
              <Button
                key={row.outcome}
                type="button"
                variant="outline"
                data-outcome={row.outcome}
                // An empty snapshot is a list of nobody; a live one may still fill.
                disabled={make.isPending || (row.count === 0 && kind === 'static')}
                onClick={() => make.mutate(row)}
              >
                {capitalized(row.label)} ({row.count}) → new list
              </Button>
            ))}
          </div>
        )}
        <p className="text-xs text-muted-foreground">
          &ldquo;No reply&rdquo; is everyone still active or finished without answering. It leaves
          out the people who bounced, opted out or were removed, which have their own lists.
          &ldquo;Replied&rdquo; counts responses you recorded by hand.
        </p>

        {make.isError && (
          <Callout tone="danger" title="The list was not made.">
            <p>{errorText(make.error)}</p>
          </Callout>
        )}
        {made !== null && !make.isError && (
          <Callout tone="info" title={`Made “${made.name}”`}>
            <p>
              {made.members} {made.members === 1 ? 'contact' : 'contacts'}
              {made.kind === 'smart' ? ' now; it updates as the campaign moves on' : ''}.{' '}
              <Link
                to="/campaigns/new"
                search={{ list: made.list_id }}
                className="underline underline-offset-4"
              >
                Start a campaign from it
              </Link>{' '}
              or{' '}
              <Link to="/lists" className="underline underline-offset-4">
                open the lists
              </Link>
              .
            </p>
          </Callout>
        )}
      </CardContent>
    </Card>
  )
}
