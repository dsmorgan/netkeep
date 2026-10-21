import { describe, expect, it } from 'vitest'

import type { SetupStep } from './setup-api'
import {
  STEP_EXPLANATIONS,
  actionLabel,
  currentStep,
  groupSteps,
  groupSummary,
  markLook,
  settledCount,
  shownTitle,
  skipAction,
  startsFolded,
  stateLabel,
} from './setup-steps'
import { GRAPH, step } from './steps-fixtures'

/** The server's graph with some states set, the rest left at to do. */
function graph(states: Record<string, Partial<SetupStep>> = {}): SetupStep[] {
  return GRAPH.map((each) => ({ ...each, ...states[each.key] }))
}

const connect = (states: Record<string, Partial<SetupStep>>) => {
  const group = groupSteps(graph(states)).find((each) => each.key === 'connect')
  if (group === undefined) throw new Error('no connect group')
  return group
}

describe('groupSteps', () => {
  it('sorts the eleven steps into four groups, in number order', () => {
    const groups = groupSteps([...graph()].reverse())
    expect(groups.map((group) => group.title)).toEqual([
      'Your account',
      'Connect at least one',
      'Start reaching out',
      'Optional',
    ])
    expect(groups.flatMap((group) => group.items.map((each) => each.number))).toEqual([
      1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11,
    ])
    const tracks = groups[1]?.tracked
    expect(tracks?.map((track) => [track.title, track.items.map((each) => each.number)])).toEqual([
      ['Gmail', [3]],
      ['LinkedIn', [4, 5, 6, 7]],
    ])
  })

  it('keeps a step no group names, in a last group of its own', () => {
    const groups = groupSteps([...graph(), step('newer', { number: 12, title: 'Newer' })])
    expect(groups.at(-1)?.title).toBe('More steps')
    expect(groups.at(-1)?.items.map((each) => each.key)).toEqual(['newer'])
  })

  it('has an explanation for every step', () => {
    expect(GRAPH.filter((each) => STEP_EXPLANATIONS[each.key] === undefined)).toEqual([])
  })
})

describe('progress and the step to do now', () => {
  it('counts every step done or skipped, the optional one too', () => {
    const steps = graph({
      about_you: { state: 'done' },
      gmail: { state: 'skipped', skipped: true },
      login_item: { state: 'skipped', skipped: true },
      triage: { state: 'in_progress' },
    })
    expect(settledCount(steps)).toBe(3)
  })

  it('picks the lowest numbered next, else in progress, else the first to do', () => {
    const blocked = { state: 'blocked' as const }
    expect(currentStep(graph({ import: { next: true }, about_you: { state: 'done' } }))?.key).toBe(
      'import',
    )
    expect(
      currentStep(
        graph({ about_you: { state: 'done' }, import: blocked, triage: { state: 'in_progress' } }),
      )?.key,
    ).toBe('triage')
    expect(currentStep(graph({ about_you: { state: 'done' } }))?.key).toBe('import')
    const finished = GRAPH.map((each) => ({ ...each, state: 'done' as const }))
    expect(currentStep(finished)).toBeUndefined()
  })
})

describe('group summaries', () => {
  it('says "✓ Done" and starts folded once every step is done or skipped', () => {
    const group = groupSteps(
      graph({ about_you: { state: 'done' }, import: { state: 'skipped' } }),
    )[0]!
    expect(groupSummary(group)).toEqual({ text: '✓ Done', ok: true })
    expect(startsFolded(group)).toBe(true)
  })

  it('counts the rest, and leaves them open', () => {
    const group = groupSteps(graph({ triage: { state: 'done' } }))[2]!
    expect(groupSummary(group)).toEqual({ text: '1 of 3', ok: false })
    expect(startsFolded(group)).toBe(false)
  })

  it('says whether a track of "Connect at least one" is finished', () => {
    expect(groupSummary(connect({}))).toEqual({ text: 'None connected yet', ok: false })
    // Skipping Gmail connects nothing.
    expect(groupSummary(connect({ gmail: { state: 'skipped' } })).text).toBe('None connected yet')
    // Half the LinkedIn track is not a track finished.
    expect(groupSummary(connect({ chrome: { state: 'done' } })).text).toBe('None connected yet')
    expect(groupSummary(connect({ gmail: { state: 'done' } }))).toEqual({
      text: '✓ One connected',
      ok: true,
    })
    const linkedin = {
      chrome: { state: 'done' as const },
      first_sync: { state: 'done' as const },
      first_inbox: { state: 'done' as const },
      arm_schedule: { state: 'skipped' as const },
    }
    expect(groupSummary(connect(linkedin)).text).toBe('✓ One connected')
    expect(groupSummary(connect({ ...linkedin, gmail: { state: 'skipped' } })).text).toBe('✓ Done')
  })
})

describe('rows', () => {
  it('marks done, next and in progress, waiting and skipped apart', () => {
    expect(markLook(step('a', { state: 'done', skipped: true }))).toBe('done')
    expect(markLook(step('a', { state: 'in_progress' }))).toBe('next')
    expect(markLook(step('a', { state: 'todo', next: true }))).toBe('next')
    expect(markLook(step('a', { state: 'todo' }))).toBe('todo')
    expect(markLook(step('a', { state: 'blocked', skipped: true }))).toBe('waiting')
    expect(markLook(step('a', { state: 'skipped', skipped: true }))).toBe('skipped')
  })

  it('names each state and each link', () => {
    expect(stateLabel(step('a', { state: 'blocked' }))).toBe('Waiting')
    expect(stateLabel(step('a', { state: 'todo', next: true }))).toBe('Next')
    expect(stateLabel(step('a', { state: 'todo' }))).toBe('To do')
    expect(stateLabel(step('a', { state: 'in_progress' }))).toBe('In progress')
    expect(actionLabel(step('a', { state: 'todo' }))).toBe('Start')
    expect(actionLabel(step('a', { state: 'blocked' }))).toBe('Start')
    expect(actionLabel(step('a', { state: 'skipped' }))).toBe('Start')
    expect(actionLabel(step('a', { state: 'in_progress' }))).toBe('Continue')
    expect(actionLabel(step('a', { state: 'done' }))).toBe('Open')
  })

  it('offers Skip and Undo skip only on a skippable step that isn’t done', () => {
    expect(skipAction(step('a', { skippable: false }))).toBeNull()
    expect(skipAction(step('a', { skippable: false, skipped: true }))).toBeNull()
    expect(skipAction(step('a', { state: 'done', skipped: true }))).toBeNull()
    expect(skipAction(step('a'))).toBe('skip')
    expect(skipAction(step('a', { state: 'skipped', skipped: true }))).toBe('undo')
    // A waiting step offers only to undo a skip it has.
    expect(skipAction(step('a', { state: 'blocked' }))).toBeNull()
    expect(skipAction(step('a', { state: 'blocked', skipped: true }))).toBe('undo')
    // Skipped with 4: its skip is undone at 4.
    expect(skipAction(step('a', { state: 'skipped', skipped: false }))).toBeNull()
  })

  it('drops "(optional)" from the optional step’s title, which its badge says', () => {
    expect(shownTitle(step('a', { title: 'Run at login (optional)', optional: true }))).toBe(
      'Run at login',
    )
    expect(shownTitle(step('a', { title: 'Triage (optional)' }))).toBe('Triage (optional)')
  })
})
