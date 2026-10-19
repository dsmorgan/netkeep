import { afterEach, describe, expect, it, vi } from 'vitest'

import { GRAPH, step } from './steps-fixtures'
import {
  BOX_TONE,
  TAB_STORAGE_KEY,
  boxLook,
  defaultTab,
  layoutSteps,
  needsText,
  stateLabel,
  storeTab,
  storedTab,
} from './steps-layout'

describe('layoutSteps', () => {
  it('puts each step one column past its deepest prerequisite', () => {
    const layout = layoutSteps(GRAPH)
    const column = Object.fromEntries(layout.placed.map((p) => [p.step.key, p.column]))
    expect(layout.columns).toBe(3)
    expect(column).toEqual({
      about_you: 0,
      import: 0,
      gmail: 0,
      chrome: 0,
      login_item: 0,
      first_sync: 1,
      first_inbox: 1,
      triage: 1,
      build_list: 1,
      campaign: 1,
      arm_schedule: 2,
    })
  })

  it('lists steps column by column with unique rows, and every prerequisite before its step', () => {
    const layout = layoutSteps(GRAPH)
    const order = layout.placed.map((p) => p.step.key)
    for (const item of GRAPH) {
      for (const parent of [...item.needs, ...item.needs_any]) {
        expect(order.indexOf(parent)).toBeLessThan(order.indexOf(item.key))
      }
    }
    for (let column = 0; column < layout.columns; column += 1) {
      const rows = layout.placed.filter((p) => p.column === column).map((p) => p.row)
      expect(new Set(rows).size).toBe(rows.length)
      expect(Math.max(...rows)).toBeLessThan(layout.rows)
    }
    // The schedule sits level with the sync it waits for.
    const row = (key: string) => layout.placed.find((p) => p.step.key === key)?.row
    expect(row('arm_schedule')).toBe(row('first_sync'))
  })

  it('draws an arrow from every prerequisite, dashed for a one-of', () => {
    const edges = layoutSteps(GRAPH).edges
    expect(edges).toHaveLength(8)
    expect(
      edges.filter((edge) => edge.to === 'campaign').map(({ from, any }) => ({ from, any })),
    ).toEqual([
      { from: 'import', any: false },
      { from: 'gmail', any: true },
      { from: 'chrome', any: true },
    ])
  })

  it('spreads arrows that share a box so none land on the same point', () => {
    const edges = layoutSteps(GRAPH).edges
    const into = edges.filter((edge) => edge.to === 'campaign')
    // Ordered by the row of the box they come from: import, gmail, chrome.
    expect(into.map((edge) => edge.toAt)).toEqual([0.25, 0.5, 0.75])
    const fromChrome = edges.filter((edge) => edge.from === 'chrome')
    expect(new Set(fromChrome.map((edge) => edge.fromAt)).size).toBe(fromChrome.length)
    const single = edges.find((edge) => edge.to === 'arm_schedule')
    expect(single).toMatchObject({ fromAt: 0.5, toAt: 0.5 })
  })

  it('survives an empty answer and a prerequisite it does not know', () => {
    expect(layoutSteps([])).toEqual({ placed: [], columns: 0, rows: 0, edges: [] })
    const layout = layoutSteps([step('a', { needs: ['gone'] })])
    expect(layout.placed[0]).toMatchObject({ column: 0, row: 0 })
    expect(layout.edges).toEqual([])
  })
})

describe('box looks', () => {
  it('lights done, highlights next and in progress, and sets not-ready and skipped apart', () => {
    expect(boxLook(step('a', { state: 'done', skipped: true }))).toBe('done')
    expect(boxLook(step('a', { state: 'in_progress' }))).toBe('next')
    expect(boxLook(step('a', { state: 'todo', next: true }))).toBe('next')
    expect(boxLook(step('a', { state: 'todo' }))).toBe('open')
    expect(boxLook(step('a', { state: 'blocked', skipped: true }))).toBe('waiting')
    expect(boxLook(step('a', { state: 'skipped', skipped: true }))).toBe('skipped')
    expect(BOX_TONE).toEqual({
      done: 'ok',
      next: 'accent',
      waiting: undefined,
      skipped: undefined,
      open: undefined,
    })
  })

  it('names each state', () => {
    expect(stateLabel(step('a', { state: 'blocked' }))).toBe('Not ready')
    expect(stateLabel(step('a', { state: 'todo', next: true }))).toBe('Next')
    expect(stateLabel(step('a', { state: 'todo' }))).toBe('To do')
    expect(stateLabel(step('a', { state: 'in_progress' }))).toBe('In progress')
  })
})

describe('the default tab', () => {
  afterEach(() => {
    window.localStorage.clear()
    vi.restoreAllMocks()
  })

  it('opens Steps until setup is complete, then Overview', () => {
    expect(defaultTab(null, { complete: false }, false)).toBe('steps')
    expect(defaultTab(null, { complete: true }, false)).toBe('overview')
  })

  it('keeps the last pick whatever setup says', () => {
    expect(defaultTab('overview', { complete: false }, false)).toBe('overview')
    expect(defaultTab('steps', { complete: true }, false)).toBe('steps')
  })

  it('opens Overview when setup cannot be read', () => {
    expect(defaultTab(null, undefined, true)).toBe('overview')
  })

  it('remembers the pick in this browser, and shrugs off storage that refuses', () => {
    expect(storedTab()).toBeNull()
    storeTab('overview')
    expect(window.localStorage.getItem(TAB_STORAGE_KEY)).toBe('overview')
    expect(storedTab()).toBe('overview')
    window.localStorage.setItem(TAB_STORAGE_KEY, 'something else')
    expect(storedTab()).toBeNull()
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('denied')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('denied')
    })
    expect(storedTab()).toBeNull()
    expect(() => storeTab('steps')).not.toThrow()
  })
})

describe('needsText', () => {
  const titles = new Map([
    ['import', 'Import contacts'],
    ['gmail', 'Connect Gmail'],
    ['chrome', 'Chrome'],
  ])

  it('keeps "one of" for a one-of prerequisite', () => {
    const campaign = step('campaign', { needs: ['import'], needs_any: ['gmail', 'chrome'] })
    expect(needsText(campaign, titles)).toBe(
      'Needs: Import contacts, and one of Connect Gmail or Chrome',
    )
    expect(needsText(step('x', { needs_any: ['gmail', 'chrome'] }), titles)).toBe(
      'Needs one of: Connect Gmail or Chrome',
    )
    expect(needsText(step('x', { needs: ['import'] }), titles)).toBe('Needs: Import contacts')
  })

  it('says nothing when the reason already does, or there is nothing to need', () => {
    expect(needsText(step('x', { needs: ['import'], state: 'blocked' }), titles)).toBeNull()
    expect(needsText(step('x', { needs: ['import'], state: 'done' }), titles)).toBeNull()
    expect(needsText(step('x'), titles)).toBeNull()
  })
})
