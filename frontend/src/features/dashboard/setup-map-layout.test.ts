import { describe, expect, it } from 'vitest'

import { GRAPH, step } from './steps-fixtures'
import {
  BOX_WIDTH,
  MAP_STEPS,
  TIP_GAP,
  WIRES,
  actionLabel,
  bottom,
  boxLook,
  centerX,
  centerY,
  fitsMap,
  placeMap,
  right,
  roundedPath,
  routePoints,
  routeWires,
  skipAction,
  stateLabel,
  wireLook,
  type MapNode,
  type Point,
  type Rect,
  type WireDef,
} from './setup-map-layout'

const overlaps = (a: Rect, b: Rect) =>
  a.left < right(b) && b.left < right(a) && a.top < bottom(b) && b.top < bottom(a)
const inside = (inner: Rect, outer: Rect) =>
  inner.left >= outer.left &&
  inner.top >= outer.top &&
  right(inner) <= right(outer) &&
  bottom(inner) <= bottom(outer)

describe('placeMap', () => {
  const placement = placeMap({})
  const { nodes, groups } = placement

  it('lays row 1 out left to right and row 2 right to left, with no box on another', () => {
    const all = Object.values(nodes)
    all.forEach((a, i) => all.slice(i + 1).forEach((b) => expect(overlaps(a, b)).toBe(false)))
    expect(nodes.about_you.left).toBeLessThan(nodes.import.left)
    expect(nodes.import.left).toBeLessThan(nodes.gmail.left)
    expect(nodes.gmail.left).toBe(nodes.chrome.left)
    expect(nodes.chrome.left).toBeLessThan(nodes.first_sync.left)
    expect(nodes.first_sync.left).toBe(nodes.first_inbox.left)
    expect(nodes.first_sync.left).toBeLessThan(nodes.arm_schedule.left)
    // Row 2: 8 under 7, then leftward to 11 and Ready.
    const row = ['triage', 'build_list', 'campaign', 'login_item'] as const
    row.forEach((key) => expect(nodes[key].top).toBe(nodes.triage.top))
    expect(nodes.triage.left).toBe(nodes.arm_schedule.left)
    for (let index = 1; index < row.length; index += 1) {
      expect(nodes[row[index] as MapNode].left).toBeLessThan(nodes[row[index - 1] as MapNode].left)
    }
    expect(right(nodes.ready)).toBeLessThan(nodes.login_item.left)
    expect(nodes.triage.top).toBeGreaterThan(bottom(groups.linkedin))
    MAP_STEPS.forEach((key) => expect(nodes[key].width).toBe(BOX_WIDTH))
  })

  it('keeps straight runs level: 1 with 2, 4 and 7 with the middle of 5 and 6', () => {
    expect(centerY(nodes.about_you)).toBe(centerY(nodes.import))
    const middle = (nodes.first_sync.top + bottom(nodes.first_inbox)) / 2
    expect(centerY(nodes.chrome)).toBe(middle)
    expect(centerY(nodes.arm_schedule)).toBe(middle)
    // The fork sits halfway between the two tracks it leads to.
    expect(centerY(nodes.import)).toBe((centerY(nodes.gmail) + centerY(nodes.chrome)) / 2)
    // Start is above step 1, Ready beside step 11.
    expect(centerX(nodes.start)).toBe(centerX(nodes.about_you))
    expect(bottom(nodes.start)).toBeLessThan(nodes.about_you.top)
    expect(centerY(nodes.ready)).toBe(centerY(nodes.login_item))
  })

  it('draws the groups round their tracks and the lane past the LinkedIn group', () => {
    expect(inside(nodes.gmail, groups.gmail)).toBe(true)
    for (const key of ['chrome', 'first_sync', 'first_inbox', 'arm_schedule'] as const) {
      expect(inside(nodes[key], groups.linkedin)).toBe(true)
    }
    expect(overlaps(groups.gmail, groups.linkedin)).toBe(false)
    expect(overlaps(placement.or, groups.linkedin)).toBe(false)
    expect(placement.laneX).toBeGreaterThan(right(groups.linkedin))
    expect(placement.width).toBeGreaterThan(placement.laneX)
    // Everything is on the canvas.
    for (const rect of [...Object.values(nodes), groups.gmail, groups.linkedin, placement.or]) {
      expect(rect.top).toBeGreaterThanOrEqual(0)
      expect(rect.left).toBeGreaterThanOrEqual(0)
      expect(bottom(rect)).toBeLessThan(placement.height)
    }
  })

  it('uses the measured heights, so a taller box pushes the rest apart', () => {
    const tall = placeMap({ first_sync: { width: BOX_WIDTH, height: 160 } })
    expect(tall.nodes.first_sync.height).toBe(160)
    expect(tall.nodes.first_inbox.top).toBeGreaterThan(nodes.first_inbox.top + 50)
    expect(tall.nodes.triage.top).toBeGreaterThan(nodes.triage.top + 50)
    expect(tall.height).toBeGreaterThan(placement.height + 50)
    // A 0 is no measurement (jsdom): the default size stands.
    expect(placeMap({ gmail: { width: 0, height: 0 } }).nodes.gmail).toEqual(nodes.gmail)
  })

  it('places only the server’s eleven steps', () => {
    expect(fitsMap(GRAPH)).toBe(true)
    expect(fitsMap(GRAPH.slice(1))).toBe(false)
    expect(fitsMap([...GRAPH.slice(1), step('newer_step')])).toBe(false)
  })
})

/** Invented rects: 2 forks to 3 and 4, 4 forks to 5 and 6, which join at 7. */
function rects(): Record<MapNode, Rect> {
  const box = (left: number, top: number): Rect => ({ left, top, width: 140, height: 80 })
  return {
    start: { left: 48, top: 10, width: 84, height: 30 },
    about_you: box(20, 100),
    import: box(216, 100),
    gmail: box(412, 20),
    chrome: box(412, 200),
    first_sync: box(608, 150),
    first_inbox: box(608, 250),
    arm_schedule: box(804, 200),
    triage: box(804, 400),
    build_list: box(608, 400),
    campaign: box(412, 400),
    login_item: box(216, 400),
    ready: { left: 48, top: 425, width: 84, height: 30 },
  }
}

const wire = (from: MapNode, to: MapNode) =>
  WIRES.find((def) => def.from === from && def.to === to) as WireDef
const points = (from: MapNode, to: MapNode) => routePoints(wire(from, to), rects(), 978)

describe('routePoints', () => {
  it('runs every connector in right angles, from its source to just short of its target', () => {
    const nodes = rects()
    for (const def of WIRES) {
      const route = routePoints(def, nodes, 978)
      for (let index = 1; index < route.length; index += 1) {
        const [x0, y0] = route[index - 1] as Point
        const [x1, y1] = route[index] as Point
        expect(x0 === x1 || y0 === y1).toBe(true)
      }
      const [x, y] = route.at(-1) as Point
      const target = nodes[def.to]
      if (def.kind === 'across') expect([x, y]).toEqual([target.left - TIP_GAP, centerY(target)])
      if (def.kind === 'down') expect([x, y]).toEqual([centerX(target), target.top - TIP_GAP])
      if (def.kind === 'back' || def.kind === 'lane')
        expect([x, y]).toEqual([right(target) + TIP_GAP, centerY(target)])
    }
  })

  it('gives a fan-out one shared stem before it splits', () => {
    // 2 (right edge 356, middle 140) to 3 and 4 (left edge 412): one stem to x 384.
    expect(points('import', 'gmail')).toEqual([
      [356, 140],
      [384, 140],
      [384, 60],
      [411, 60],
    ])
    expect(points('import', 'chrome')).toEqual([
      [356, 140],
      [384, 140],
      [384, 240],
      [411, 240],
    ])
    expect(points('chrome', 'first_sync').slice(0, 2)).toEqual(
      points('chrome', 'first_inbox').slice(0, 2),
    )
  })

  it('merges a join into one line before its target', () => {
    // 5 and 6 (right edge 748) into 7 (left edge 804, middle 240): one line from x 776.
    expect(points('first_sync', 'arm_schedule').slice(-2)).toEqual([
      [776, 240],
      [803, 240],
    ])
    expect(points('first_inbox', 'arm_schedule').slice(-2)).toEqual([
      [776, 240],
      [803, 240],
    ])
  })

  it('takes a fan-out’s stem to its nearest target when the targets don’t line up', () => {
    const nodes = rects()
    nodes.chrome = { ...nodes.chrome, left: 500 }
    const route = routePoints(wire('import', 'chrome'), nodes, 978)
    // Halfway from 2's right edge (356) to the nearer target's left edge (412), not to 500.
    expect(route[1]).toEqual([384, 140])
  })

  it('runs Gmail’s lane round the right and into 8’s right side, and 7 straight down', () => {
    expect(points('gmail', 'triage')).toEqual([
      [552, 60],
      [978, 60],
      [978, 440],
      [945, 440],
    ])
    expect(points('arm_schedule', 'triage')).toEqual([
      [874, 280],
      [874, 340],
      [874, 340],
      [874, 399],
    ])
    // Start drops into step 1's top.
    expect(points('start', 'about_you').at(-1)).toEqual([90, 99])
    // Row 2 runs leftward, ending at Ready's right edge.
    expect(points('login_item', 'ready').at(-1)).toEqual([133, 440])
  })
})

describe('roundedPath', () => {
  it('rounds each corner with a quadratic curve', () => {
    expect(
      roundedPath([
        [356, 140],
        [384, 140],
        [384, 60],
        [411, 60],
      ]),
    ).toBe('M356,140 L374,140 Q384,140 384,130 L384,70 Q384,60 394,60 L411,60')
  })

  it('shrinks the radius to fit a short run, and drops repeated and straight-through points', () => {
    expect(
      roundedPath([
        [0, 0],
        [8, 0],
        [8, 40],
      ]),
    ).toBe('M0,0 L4,0 Q8,0 8,4 L8,40')
    expect(
      roundedPath([
        [874, 280],
        [874, 340],
        [874, 340],
        [874, 399],
      ]),
    ).toBe('M874,280 L874,399')
    expect(roundedPath([])).toBe('')
  })

  it('draws a path for every connector on a placed map', () => {
    const wires = routeWires(placeMap({}))
    expect(wires).toHaveLength(WIRES.length)
    wires.forEach((each) => expect(each.d).toMatch(/^M[\d.]+,[\d.]+( [LQ][\d., ]+)+$/))
  })
})

describe('looks', () => {
  it('fills done, tints next and in progress, and sets waiting and skipped apart', () => {
    expect(boxLook(step('a', { state: 'done', skipped: true }))).toBe('done')
    expect(boxLook(step('a', { state: 'in_progress' }))).toBe('next')
    expect(boxLook(step('a', { state: 'todo', next: true }))).toBe('next')
    expect(boxLook(step('a', { state: 'todo' }))).toBe('todo')
    expect(boxLook(step('a', { state: 'blocked', skipped: true }))).toBe('waiting')
    expect(boxLook(step('a', { state: 'skipped', skipped: true }))).toBe('skipped')
  })

  it('names each state and each link', () => {
    expect(stateLabel(step('a', { state: 'blocked' }))).toBe('Waiting')
    expect(stateLabel(step('a', { state: 'todo', next: true }))).toBe('Next')
    expect(stateLabel(step('a', { state: 'todo' }))).toBe('To do')
    expect(stateLabel(step('a', { state: 'in_progress' }))).toBe('In progress')
    expect(actionLabel(step('a', { state: 'todo' }))).toBe('Start')
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

  it('lights connectors green after reached steps and moves the accent into the next one', () => {
    const steps = new Map(
      [
        step('about_you', { state: 'done' }),
        step('import', { state: 'done' }),
        step('gmail', { state: 'skipped', skipped: true }),
        step('chrome', { state: 'todo', next: true }),
        step('first_sync', { state: 'blocked' }),
        step('login_item', { state: 'todo' }),
      ].map((each) => [each.key, each]),
    )
    const look = (from: MapNode, to: MapNode, complete = false) =>
      wireLook(wire(from, to), steps, complete)
    expect(look('start', 'about_you')).toBe('done')
    expect(look('about_you', 'import')).toBe('done')
    expect(look('import', 'gmail')).toBe('done')
    expect(look('import', 'chrome')).toBe('next')
    expect(look('chrome', 'first_sync')).toBe('base')
    expect(look('login_item', 'ready')).toBe('base')
    expect(look('login_item', 'ready', true)).toBe('done')

    const fresh = new Map(
      [step('about_you', { next: true }), step('import')].map((s) => [s.key, s]),
    )
    expect(wireLook(wire('start', 'about_you'), fresh, false)).toBe('next')
    expect(wireLook(wire('about_you', 'import'), fresh, false)).toBe('base')
    // The "one or both" pair is dotted while it's not reached.
    expect(wireLook(wire('import', 'gmail'), fresh, false)).toBe('alt')
  })
})
