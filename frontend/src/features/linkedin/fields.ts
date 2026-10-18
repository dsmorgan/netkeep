import { OUTCOME_TEXT, isPrefillOutcome } from '../linkedin-steps/prefill-copy'

import type { RunStatus } from './types'

export const RUN_STATUS_CLASSES: Record<RunStatus, string> = {
  running: 'bg-accent-soft text-accent-ink',
  completed: 'bg-ok-soft text-ok-ink',
  aborted: 'bg-warn-soft text-warn-ink',
  failed: 'bg-destructive/10 text-destructive',
}

/**
 * How a run ended, in plain words: the server's `stop_reason_text` ("outside active
 * hours"), falling back to the stored reason for one it has no words for yet (#213).
 */
export function stopReasonLabel(run: {
  stop_reason: string | null
  stop_reason_text: string | null
}): string | null {
  // A prefill run's outcomes (`not_typed`, `partially_typed`, ...) have no words on the
  // server yet; its `error` says why, and the queue says what to do.
  if (isPrefillOutcome(run.stop_reason) && run.stop_reason_text === run.stop_reason) {
    return OUTCOME_TEXT[run.stop_reason]
  }
  return run.stop_reason_text ?? run.stop_reason
}

/** A timestamp in the reader's own locale, or the raw string if it will not parse. */
export function formatWhen(iso: string | null): string {
  if (iso === null) return '—'
  const when = new Date(iso)
  if (Number.isNaN(when.getTime())) return iso
  return when.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

/** One line of a formatted progress or counts object. */
export interface Field {
  label: string
  value: string
}

/** A raw value as a short, readable string: not a name, not a URN — this is `progress`/`counts`. */
function display(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  if (typeof value === 'number' || typeof value === 'string') return String(value)
  return JSON.stringify(value)
}

function titleCase(key: string): string {
  return key
    .split('_')
    .map((word) => (word.length === 0 ? word : word[0]?.toUpperCase() + word.slice(1)))
    .join(' ')
}

/**
 * The order the two known progress/counts shapes are shown in (spec 9.4, 9.10's
 * `ProgressEvent`): enrichment's (`planned`, `visited`, `harvested`, `not_found`,
 * `unreadable`, `stopped`) or a connections sync's (`mode`, `pages`, `connections`,
 * `total`, `stopped`; a finished sync's counts also carry `hidden`, the places
 * LinkedIn's total counts that no answer held a card for, #515, and
 * `rescued_not_whole`, answers read from a streamed copy the flight check did
 * not find whole, #525). A finished run of either kind also carries `snapshots` and
 * `field_changes`, how many times it changed each job field (#518), shown after those.
 * Both shapes are `dict[str, Any]` on the wire — untyped on purpose,
 * since a job's own progress dataclass is free to change — so this only orders
 * whichever of these keys shows up and appends anything else after, rather than
 * assuming one shape and dropping what does not fit it.
 */
const KNOWN_ORDER = [
  'mode',
  'kind',
  'planned',
  'visited',
  'pages',
  'connections',
  'harvested',
  'not_found',
  'unreadable',
  'total',
  'hidden',
  'rescued_not_whole',
  'snapshots',
  'field_changes',
  'stopped',
]

/**
 * Per-visit and per-answer records (#405) that live in `counts`/`progress` but are
 * lists, not counts: the run detail shows them as their own table from
 * `GET /linkedin/runs/{id}/diagnostics`, never as a JSON blob in a field.
 */
const RECORD_KEYS = ['unreadable_visits', 'lost']

/** `progress`/`counts` (both `Record<string, unknown> | null`) as labeled, ordered lines. */
export function formatFields(data: Record<string, unknown> | null | undefined): Field[] {
  if (data === null || data === undefined) return []
  const keys = Object.keys(data).filter(
    (key) => !(RECORD_KEYS.includes(key) && Array.isArray(data[key])),
  )
  const ordered = [
    ...KNOWN_ORDER.filter((key) => keys.includes(key)),
    ...keys.filter((key) => !KNOWN_ORDER.includes(key)).sort(),
  ]
  return ordered.flatMap((key) => {
    const value = data[key]
    // A nested count object (a run's `harvests`, #465) becomes one labeled row per entry,
    // "Harvests Applied: 3", never a JSON blob. Any key works, so new counts need no change here.
    if (isPlainObject(value)) {
      const entries = Object.entries(value)
      if (entries.length === 0) return [{ label: titleCase(key), value: '—' }]
      return formatFields(value).map((field) => ({
        label: `${titleCase(key)} ${field.label}`,
        value: field.value,
      }))
    }
    return [{ label: titleCase(key), value: display(value) }]
  })
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** The first few fields of `progress`/`counts`, as one line for a table cell. */
export function summarizeFields(data: Record<string, unknown> | null | undefined, max = 3): string {
  const fields = formatFields(data).slice(0, max)
  if (fields.length === 0) return '—'
  return fields.map((field) => `${field.label.toLowerCase()}: ${field.value}`).join(', ')
}
