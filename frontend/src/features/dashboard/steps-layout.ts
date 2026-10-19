/**
 * The Steps tab's pure parts: where each box sits in the flow diagram, which tab the
 * dashboard opens on, and how each box looks. Kept apart from the components so the
 * logic is tested without rendering.
 */
import type { Tone } from '@/components/ui/tone'

import type { Setup, SetupStep } from './setup-api'

export type DashboardTab = 'steps' | 'overview'

export const TAB_STORAGE_KEY = 'netkeeper.dashboard.tab'

/** The tab picked last in this browser, or null. Storage can be missing or refuse. */
export function storedTab(): DashboardTab | null {
  try {
    const value = window.localStorage.getItem(TAB_STORAGE_KEY)
    return value === 'steps' || value === 'overview' ? value : null
  } catch {
    return null
  }
}

export function storeTab(tab: DashboardTab): void {
  try {
    window.localStorage.setItem(TAB_STORAGE_KEY, tab)
  } catch {
    // A browser that refuses storage just doesn't remember the pick.
  }
}

/**
 * The tab to open: the person's last pick, else Steps until every required step is
 * done or skipped (`complete`), then Overview. When setup can't be read (another
 * account, the backend down), Overview, which says what it can on its own.
 */
export function defaultTab(
  picked: DashboardTab | null,
  setup: Pick<Setup, 'complete'> | undefined,
  failed: boolean,
): DashboardTab {
  if (picked !== null) return picked
  if (failed || setup === undefined) return 'overview'
  return setup.complete ? 'overview' : 'steps'
}

export interface Placed {
  step: SetupStep
  /** 0-based column: how many steps deep its longest chain of prerequisites is. */
  column: number
  /** 0-based row within the diagram. */
  row: number
}

export interface Layout {
  /** In reading order: column by column, top to bottom. The DOM and tab order follow it. */
  placed: Placed[]
  columns: number
  rows: number
  /** `from` must be done first; `any` marks a one-of prerequisite. */
  edges: Edge[]
}

export interface Edge {
  from: string
  to: string
  any: boolean
  /**
   * Where the arrow meets each box, as a fraction of its height. Arrows that share
   * a box are spread across it in the order of the box at their other end, so a
   * dashed one-of arrow never lands on the same point as a solid one.
   */
  fromAt: number
  toAt: number
}

function prerequisites(step: SetupStep): string[] {
  return [...step.needs, ...step.needs_any]
}

/**
 * A left-to-right layered layout. A step's column is one past its deepest
 * prerequisite's. Within a column, steps sort by the average row of their
 * prerequisites, so arrows mostly run straight, and each takes the row nearest that
 * average that is still free and leaves room for the steps after it.
 */
export function layoutSteps(steps: SetupStep[]): Layout {
  const byKey = new Map(steps.map((step) => [step.key, step]))
  const depth = new Map<string, number>()
  const depthOf = (key: string, seen: Set<string>): number => {
    const known = depth.get(key)
    if (known !== undefined) return known
    const step = byKey.get(key)
    if (step === undefined || seen.has(key)) return 0
    seen.add(key)
    const parents = prerequisites(step).filter((parent) => byKey.has(parent))
    const value =
      parents.length === 0 ? 0 : 1 + Math.max(...parents.map((parent) => depthOf(parent, seen)))
    depth.set(key, value)
    return value
  }
  steps.forEach((step) => depthOf(step.key, new Set()))

  const columns = steps.length === 0 ? 0 : Math.max(...steps.map((s) => depth.get(s.key) ?? 0)) + 1
  const groups: SetupStep[][] = Array.from({ length: columns }, () => [])
  steps.forEach((step) => groups[depth.get(step.key) ?? 0]?.push(step))
  const rows = Math.max(0, ...groups.map((group) => group.length))

  const rowOf = new Map<string, number>()
  const placed: Placed[] = []
  groups.forEach((group, column) => {
    const wanted = (step: SetupStep) => {
      const parents = prerequisites(step).filter((parent) => rowOf.has(parent))
      if (parents.length === 0) return group.indexOf(step)
      return parents.reduce((sum, parent) => sum + (rowOf.get(parent) ?? 0), 0) / parents.length
    }
    const ordered =
      column === 0 ? group : [...group].sort((left, right) => wanted(left) - wanted(right))
    let previous = -1
    ordered.forEach((step, index) => {
      const room = rows - (ordered.length - index)
      const row = Math.max(Math.min(Math.round(wanted(step)), room), previous + 1)
      previous = row
      rowOf.set(step.key, row)
      placed.push({ step, column, row })
    })
  })

  const plain = steps.flatMap((step) => [
    ...step.needs
      .filter((from) => byKey.has(from))
      .map((from) => ({ from, to: step.key, any: false })),
    ...step.needs_any
      .filter((from) => byKey.has(from))
      .map((from) => ({ from, to: step.key, any: true })),
  ])
  const row = (key: string) => rowOf.get(key) ?? 0
  const spread = (siblings: typeof plain, other: (edge: (typeof plain)[number]) => string) => {
    const ordered = [...siblings].sort((left, right) => row(other(left)) - row(other(right)))
    return (edge: (typeof plain)[number]) => (ordered.indexOf(edge) + 1) / (ordered.length + 1)
  }
  const edges: Edge[] = plain.map((edge) => ({
    ...edge,
    fromAt: spread(
      plain.filter((other) => other.from === edge.from),
      (other) => other.to,
    )(edge),
    toAt: spread(
      plain.filter((other) => other.to === edge.to),
      (other) => other.from,
    )(edge),
  }))
  return { placed, columns, rows, edges }
}

/** How a box looks, from its state: lit when done, the accent when it's next. */
export type BoxLook = 'done' | 'next' | 'waiting' | 'skipped' | 'open'

export function boxLook(step: SetupStep): BoxLook {
  if (step.state === 'done') return 'done'
  if (step.state === 'blocked') return 'waiting'
  if (step.state === 'skipped') return 'skipped'
  if (step.next || step.state === 'in_progress') return 'next'
  return 'open'
}

export const BOX_TONE: Record<BoxLook, Tone | undefined> = {
  done: 'ok',
  next: 'accent',
  waiting: undefined,
  skipped: undefined,
  open: undefined,
}

/** The state in words, for the badge and for a screen reader. */
export function stateLabel(step: SetupStep): string {
  switch (step.state) {
    case 'done':
      return 'Done'
    case 'in_progress':
      return 'In progress'
    case 'blocked':
      return 'Not ready'
    case 'skipped':
      return 'Skipped'
    case 'todo':
      return step.next ? 'Next' : 'To do'
  }
}

/** What a box's link says, by where it goes. */
export const TARGET_LABELS: Record<string, string> = {
  '/settings': 'Open Settings',
  '/imports': 'Open Import',
  '/linkedin': 'Open LinkedIn',
  '/triage': 'Open Triage',
  '/lists': 'Open Lists',
  '/campaigns': 'Open Campaigns',
}

/**
 * What a box says it needs, for the narrow layout, which has no arrows: "Needs: A
 * and B", "Needs one of: A or B", or both. Null when there is nothing to say, or the
 * reason already says what the step waits for.
 */
export function needsText(step: SetupStep, titles: Map<string, string>): string | null {
  if (step.state === 'done' || step.state === 'blocked') return null
  const name = (key: string) => titles.get(key) ?? key
  const all = step.needs.map(name).join(' and ')
  const any = step.needs_any.map(name).join(' or ')
  if (all !== '' && any !== '') return `Needs: ${all}, and one of ${any}`
  if (all !== '') return `Needs: ${all}`
  if (any !== '') return `Needs one of: ${any}`
  return null
}
