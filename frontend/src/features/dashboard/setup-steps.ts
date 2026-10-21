/**
 * The setup checklist's pure parts (#747): its groups, each step's explanation and
 * section, and the states in words. Kept apart from the component so all of it is
 * tested from given steps. Every state comes from the server (`GET /setup`); nothing
 * here recomputes a state or the gating between steps.
 */
import type { SetupStep } from './setup-api'

/** A track inside a group: Gmail or LinkedIn, in their dashed boxes. */
export interface SetupTrack {
  title: string
  steps: readonly string[]
}

export interface SetupGroupDef {
  key: string
  title: string
  steps: readonly string[]
  /** Set on "Connect at least one": its steps drawn as tracks side by side. */
  tracks?: readonly SetupTrack[]
}

/** The groups in order, each with its steps in number order (the server's graph). */
export const SETUP_GROUPS: readonly SetupGroupDef[] = [
  { key: 'account', title: 'Your account', steps: ['about_you', 'import'] },
  {
    key: 'connect',
    title: 'Connect at least one',
    steps: ['gmail', 'chrome', 'first_sync', 'first_inbox', 'arm_schedule'],
    tracks: [
      { title: 'Gmail', steps: ['gmail'] },
      { title: 'LinkedIn', steps: ['chrome', 'first_sync', 'first_inbox', 'arm_schedule'] },
    ],
  },
  { key: 'reach', title: 'Start reaching out', steps: ['triage', 'build_list', 'campaign'] },
  { key: 'optional', title: 'Optional', steps: ['login_item'] },
]

export const CONNECT_SUBTITLE =
  'Connect both to use everything netkeep does: Gmail sends email and spots replies, and LinkedIn keeps your connections current and sends messages there. A campaign needs at least one.'

/** What an open row says about its step. */
export const STEP_EXPLANATIONS: Partial<Record<string, string>> = {
  about_you: 'Your name and email signature. netkeep puts them in every message it drafts.',
  import: 'Bring in a CSV or your LinkedIn data archive. Every later step works on these contacts.',
  gmail:
    'Lets netkeep send email from your address and notice replies. Skip it if you only use LinkedIn.',
  chrome:
    'Start netkeep’s own Chrome window and sign in once. Skip it, and steps 5 to 7, if you only use Gmail.',
  first_sync:
    'Reads your connections list once. It takes about 20 minutes and runs in the background.',
  first_inbox: 'Reads your LinkedIn messages once, so netkeep can tell when someone replies.',
  arm_schedule: 'Turns on the weekly sync and the regular inbox checks, inside the hours you set.',
  triage: 'Go through your contacts with the keyboard: keep, tag or archive each one.',
  build_list: 'Pick the people a campaign will reach, by tag or by hand.',
  campaign: 'A sequence of messages to a list. Nothing sends until you review and start it.',
  login_item: 'Starts netkeep when you log in to your Mac, so scheduled runs keep going.',
}

/** The section of its page each step's link scrolls to (#739's anchors). */
export const STEP_ANCHORS: Partial<Record<string, string>> = {
  about_you: 'about-you',
  gmail: 'gmail',
  chrome: 'session',
  first_sync: 'connections',
  first_inbox: 'inbox',
  arm_schedule: 'schedule',
  login_item: 'login-item',
}

export interface SetupGroup extends SetupGroupDef {
  /** The group's steps the server sent, in number order. */
  items: SetupStep[]
  tracked?: { title: string; items: SetupStep[] }[]
}

const byNumber = (left: SetupStep, right: SetupStep) => left.number - right.number

/**
 * The server's steps sorted into the groups. A step no group names (a server newer
 * than this page) goes into a last group of its own, so it still shows.
 */
export function groupSteps(steps: readonly SetupStep[]): SetupGroup[] {
  const byKey = new Map(steps.map((step) => [step.key, step]))
  const pick = (keys: readonly string[]) =>
    keys.flatMap((key) => {
      const step = byKey.get(key)
      return step === undefined ? [] : [step]
    })
  const groups: SetupGroup[] = SETUP_GROUPS.map((group) => ({
    ...group,
    items: pick(group.steps).sort(byNumber),
    tracked: group.tracks?.map((track) => ({ title: track.title, items: pick(track.steps) })),
  }))
  const known = new Set(SETUP_GROUPS.flatMap((group) => group.steps))
  const rest = steps.filter((step) => !known.has(step.key)).sort(byNumber)
  if (rest.length > 0) {
    groups.push({ key: 'more', title: 'More steps', steps: rest.map((s) => s.key), items: rest })
  }
  return groups.filter((group) => group.items.length > 0)
}

/** Done or skipped: the step counts toward the total. */
export const settled = (step: SetupStep) => step.state === 'done' || step.state === 'skipped'

/** "N of 11 done" counts every step, the optional one too, done or skipped. */
export function settledCount(steps: readonly SetupStep[]): number {
  return steps.filter(settled).length
}

/**
 * The step to do now, which opens by default and the header names: the lowest
 * numbered `next`, else one in progress, else the first to do.
 */
export function currentStep(steps: readonly SetupStep[]): SetupStep | undefined {
  const ordered = [...steps].sort(byNumber)
  return (
    ordered.find((step) => step.next) ??
    ordered.find((step) => step.state === 'in_progress') ??
    ordered.find((step) => step.state === 'todo')
  )
}

/** A track is finished once every step in it is settled and its first is done. */
function finished(items: readonly SetupStep[]): boolean {
  return items.length > 0 && items.every(settled) && items[0]?.state === 'done'
}

/**
 * A group heading's summary: "✓ Done" once every step is done or skipped, else
 * "N of M"; "Connect at least one" says whether a track is finished instead.
 */
export function groupSummary(group: SetupGroup): { text: string; ok: boolean } {
  if (group.items.every(settled)) return { text: '✓ Done', ok: true }
  if (group.tracked !== undefined) {
    return group.tracked.some((track) => finished(track.items))
      ? { text: '✓ One connected', ok: true }
      : { text: 'None connected yet', ok: false }
  }
  return { text: `${settledCount(group.items)} of ${group.items.length}`, ok: false }
}

/** A group starts folded once every step in it is done or skipped. */
export const startsFolded = (group: SetupGroup) => group.items.every(settled)

/** How a row's mark looks, from its state. */
export type MarkLook = 'done' | 'next' | 'waiting' | 'skipped' | 'todo'

export function markLook(step: SetupStep): MarkLook {
  if (step.state === 'done') return 'done'
  if (step.state === 'blocked') return 'waiting'
  if (step.state === 'skipped') return 'skipped'
  if (step.next || step.state === 'in_progress') return 'next'
  return 'todo'
}

/** The state in words, for a screen reader. */
export function stateLabel(step: SetupStep): string {
  switch (step.state) {
    case 'done':
      return 'Done'
    case 'in_progress':
      return 'In progress'
    case 'blocked':
      return 'Waiting'
    case 'skipped':
      return 'Skipped'
    case 'todo':
      return step.next ? 'Next' : 'To do'
  }
}

/** What an open row's link says: Open a done step, Continue one in progress, else Start. */
export function actionLabel(step: SetupStep): string {
  if (step.state === 'done') return 'Open'
  if (step.state === 'in_progress') return 'Continue'
  return 'Start'
}

/**
 * What a row's skip button does, or null for none. Only a skippable step that isn't
 * done has one, and a waiting step only to undo its skip. Steps 5 to 7 that read
 * skipped because 4 is skipped have none: their skip is undone at 4.
 */
export function skipAction(step: SetupStep): 'skip' | 'undo' | null {
  if (!step.skippable || step.state === 'done') return null
  if (step.skipped) return 'undo'
  if (step.state === 'blocked' || step.state === 'skipped') return null
  return 'skip'
}

/**
 * The title as the row shows it: "Run at login (optional)" loses its "(optional)",
 * which the row's Optional badge says instead.
 */
export function shownTitle(step: SetupStep): string {
  return step.optional ? step.title.replace(/\s*\(optional\)$/i, '') : step.title
}
