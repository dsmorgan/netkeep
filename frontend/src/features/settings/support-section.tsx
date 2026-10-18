import { useQuery } from '@tanstack/react-query'
import { useId, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Callout } from '@/features/crm/controls'

import {
  type SupportLevel,
  type SupportLevelName,
  supportBundleUrl,
  supportLevelsQuery,
} from './api'

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * Settings, Support (#603): pick a level, see what it includes and leaves out, and
 * download the `.zip`. netkeeper never uploads it; you attach it to an issue (standard,
 * detailed) or send it privately (full). The full level needs its box ticked first.
 */
export function SupportSection() {
  const levels = useQuery(supportLevelsQuery)
  const [chosen, setChosen] = useState<SupportLevelName>('standard')
  const [days, setDays] = useState<number | null>(null)
  const [confirmed, setConfirmed] = useState(false)
  const daysId = useId()
  const confirmId = useId()

  return (
    <Card size="sm">
      <CardHeader>
        <h2 className="font-heading text-sm leading-snug font-bold">Support</h2>
        <CardDescription>
          A support bundle collects versions, settings, posture, breakers, recent runs and logs into
          one .zip file, so someone helping you can see what happened. netkeeper never uploads it:
          you download it and send it yourself, with screenshots. No level ever includes a secret.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {levels.isPending && <p role="status">Loading…</p>}
        {levels.isError && <p role="alert">{message(levels.error)}</p>}
        {levels.isSuccess && (
          <Picker
            levels={levels.data.levels}
            maxDays={levels.data.max_since_days}
            chosen={chosen}
            onChoose={(level) => {
              setChosen(level)
              setDays(null)
              setConfirmed(false)
            }}
            days={days}
            onDays={setDays}
            confirmed={confirmed}
            onConfirm={setConfirmed}
            daysId={daysId}
            confirmId={confirmId}
          />
        )}
      </CardContent>
    </Card>
  )
}

interface PickerProps {
  levels: SupportLevel[]
  maxDays: number
  chosen: SupportLevelName
  onChoose: (level: SupportLevelName) => void
  days: number | null
  onDays: (days: number | null) => void
  confirmed: boolean
  onConfirm: (confirmed: boolean) => void
  daysId: string
  confirmId: string
}

function Picker({
  levels,
  maxDays,
  chosen,
  onChoose,
  days,
  onDays,
  confirmed,
  onConfirm,
  daysId,
  confirmId,
}: PickerProps) {
  const level = levels.find((candidate) => candidate.level === chosen) ?? levels[0]
  if (level === undefined) return null
  const sinceDays = days ?? level.default_since_days
  const daysOk = Number.isInteger(sinceDays) && sinceDays >= 1 && sinceDays <= maxDays
  const ready = daysOk && (!level.needs_confirmation || confirmed)

  return (
    <div className="space-y-3">
      <fieldset className="space-y-1">
        <legend className="text-sm font-medium">Level</legend>
        {levels.map((candidate) => (
          <label key={candidate.level} className="flex items-start gap-2">
            <input
              type="radio"
              name="support-level"
              checked={candidate.level === level.level}
              onChange={() => onChoose(candidate.level)}
            />
            <span>
              {candidate.title} <span className="text-muted-foreground">— {candidate.summary}</span>
            </span>
          </label>
        ))}
      </fieldset>

      <div className="grid gap-2 sm:grid-cols-2">
        <div>
          <p className="font-medium">Includes</p>
          <ul className="list-disc space-y-0.5 pl-5" aria-label="Includes">
            {level.includes.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
        <div>
          <p className="font-medium">Leaves out</p>
          <ul className="list-disc space-y-0.5 pl-5" aria-label="Leaves out">
            {level.leaves_out.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      </div>

      <div className="grid max-w-48 gap-1">
        <Label htmlFor={daysId}>Days of logs and runs</Label>
        <Input
          id={daysId}
          type="number"
          min={1}
          max={maxDays}
          value={days ?? level.default_since_days}
          onChange={(event) => {
            const value = event.target.value
            onDays(value === '' ? Number.NaN : Number(value))
          }}
        />
        {!daysOk && (
          <p role="alert" className="text-xs">
            Between 1 and {maxDays} days.
          </p>
        )}
      </div>

      {level.warning !== null && (
        <Callout tone="danger" title="Real names inside">
          <p>{level.warning}</p>
          <div className="mt-2 flex items-center gap-2">
            <Checkbox
              id={confirmId}
              checked={confirmed}
              onCheckedChange={(checked) => onConfirm(checked)}
            />
            <Label htmlFor={confirmId}>
              I understand this file holds real names and identifiers, and I will send it privately.
            </Label>
          </div>
        </Callout>
      )}

      {ready ? (
        <Button
          size="sm"
          render={
            <a
              href={supportBundleUrl(level.level, sinceDays, confirmed)}
              download
              data-testid="support-download"
            >
              Download the {level.title.toLowerCase()} bundle
            </a>
          }
        />
      ) : (
        <Button size="sm" disabled>
          Download the {level.title.toLowerCase()} bundle
        </Button>
      )}
    </div>
  )
}
