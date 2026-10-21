/**
 * The setup map's pure parts (#740): where each box sits, how each connector runs,
 * and how each box and connector looks. Kept apart from the component so all of it is
 * tested from given sizes and rects, without a layout engine (jsdom has none).
 *
 * The map is the agreed picture of the server's graph (`netkeep/services/setup.py`),
 * not a general graph layout. Row 1 runs left to right: Start, 1, 2, then a fork into
 * two dashed groups, Gmail (3) and LinkedIn (4, then 5 and 6, then 7). Row 2 runs
 * right to left: 8, 9, 10, 11, Ready. Gmail joins 8 by a lane around the right of the
 * LinkedIn group; 7 joins 8 straight down.
 *
 * The component measures each box after it renders and passes the sizes to
 * {@link placeMap}, so no box height is assumed: a taller box pushes the rows apart.
 * {@link routeWires} then draws each connector from the placed rects.
 */
import type { SetupStep } from './setup-api'

// --- placement -----------------------------------------------------------------------

/** The steps the map has a place for, in number order. */
export const MAP_STEPS = [
  'about_you',
  'import',
  'gmail',
  'chrome',
  'first_sync',
  'first_inbox',
  'arm_schedule',
  'triage',
  'build_list',
  'campaign',
  'login_item',
] as const

export type MapStep = (typeof MAP_STEPS)[number]
/** Every node a connector can start or end at: the steps and the two end pills. */
export type MapNode = MapStep | 'start' | 'ready'
/** Everything the component measures: the nodes and the "one or both" label. */
export type Measured = MapNode | 'or'

export interface Size {
  width: number
  height: number
}

export interface Rect {
  left: number
  top: number
  width: number
  height: number
}

export interface Placement {
  nodes: Record<MapNode, Rect>
  groups: { gmail: Rect; linkedin: Rect }
  /** The "one or both" label between the two groups. */
  or: Rect
  /** Where the Gmail lane runs down, past the right of the LinkedIn group. */
  laneX: number
  width: number
  height: number
}

export const BOX_WIDTH = 140
/** Sizes to place by before the first measurement, and wherever one reads 0 (jsdom). */
export const DEFAULT_SIZES: Record<Measured, Size> = {
  ...(Object.fromEntries(MAP_STEPS.map((key) => [key, { width: BOX_WIDTH, height: 88 }])) as Record<
    MapStep,
    Size
  >),
  start: { width: 84, height: 30 },
  ready: { width: 84, height: 30 },
  or: { width: 84, height: 18 },
}

/** A box and the gutter after it. */
const COLUMN = BOX_WIDTH + 56
/** The canvas's margin on every side. */
const MARGIN = 20
/** A group's dashed border sits this far outside its boxes, and its label on the top edge. */
const GROUP_PAD_X = 12
const GROUP_PAD_TOP = 16
const GROUP_PAD_BOTTOM = 18
/** Between the Gmail and LinkedIn groups, and between 5 and 6 inside LinkedIn. */
const GROUP_GAP = 10
const STACK_GAP = 26
/** From the LinkedIn group's bottom to row 2. */
const BAND_GAP = 44
/** From the Start pill's bottom to step 1's top. */
const START_GAP = 56
/** The Gmail lane's distance past the LinkedIn group, and the canvas's past the lane. */
const LANE_OUT = 22
const LANE_MARGIN = 18
/** The "one or both" label sits this far above the LinkedIn group. */
const OR_GAP = 14

function sizeOf(sizes: Partial<Record<Measured, Size>>, key: Measured): Size {
  const measured = sizes[key]
  const fallback = DEFAULT_SIZES[key]
  // A 0 is no measurement: jsdom, or a node not rendered yet.
  return {
    width: measured !== undefined && measured.width > 0 ? measured.width : fallback.width,
    height: measured !== undefined && measured.height > 0 ? measured.height : fallback.height,
  }
}

const column = (index: number) => MARGIN + index * COLUMN

/**
 * Every node's rect on the canvas, from the measured sizes. Rows and groups grow with
 * their tallest box, and steps 1, 2, 4 and 7 sit level with what they connect to, so
 * their connectors run straight.
 */
export function placeMap(sizes: Partial<Record<Measured, Size>>): Placement {
  const size = (key: Measured) => sizeOf(sizes, key)
  const at = (key: Measured, left: number, top: number): Rect => ({ left, top, ...size(key) })
  const centeredOn = (key: Measured, left: number, center: number) =>
    at(key, left, center - size(key).height / 2)

  // The Gmail group first; everything else hangs off its top.
  const gmailTop = MARGIN
  const gmail = at('gmail', column(2), gmailTop + GROUP_PAD_TOP)
  const gmailGroup: Rect = {
    left: column(2) - GROUP_PAD_X,
    top: gmailTop,
    width: BOX_WIDTH + 2 * GROUP_PAD_X,
    height: GROUP_PAD_TOP + gmail.height + GROUP_PAD_BOTTOM,
  }

  // The LinkedIn group: 5 above 6, with 4 and 7 level with the middle of the pair.
  const linkedinTop = bottom(gmailGroup) + GROUP_GAP
  const sync = at('first_sync', column(3), linkedinTop + GROUP_PAD_TOP)
  const inbox = at('first_inbox', column(3), bottom(sync) + STACK_GAP)
  const pairMiddle = (sync.top + bottom(inbox)) / 2
  const chrome = centeredOn('chrome', column(2), pairMiddle)
  const arm = centeredOn('arm_schedule', column(4), pairMiddle)
  const linkedinBottom = Math.max(bottom(inbox), bottom(chrome), bottom(arm)) + GROUP_PAD_BOTTOM
  const linkedinGroup: Rect = {
    left: column(2) - GROUP_PAD_X,
    top: linkedinTop,
    width: column(4) + BOX_WIDTH + GROUP_PAD_X - (column(2) - GROUP_PAD_X),
    height: linkedinBottom - linkedinTop,
  }

  // 1 and 2 sit level with the fork's middle, halfway between 3 and 4.
  const fork = (centerY(gmail) + centerY(chrome)) / 2
  const about = centeredOn('about_you', column(0), fork)
  const imported = centeredOn('import', column(1), fork)
  const startSize = size('start')
  const start = at(
    'start',
    centerX(about) - startSize.width / 2,
    about.top - START_GAP - startSize.height,
  )

  // Row 2, right to left, under the LinkedIn group.
  const rowTop = linkedinBottom + BAND_GAP
  const triage = at('triage', column(4), rowTop)
  const list = at('build_list', column(3), rowTop)
  const campaign = at('campaign', column(2), rowTop)
  const login = at('login_item', column(1), rowTop)
  const readySize = size('ready')
  const ready = centeredOn('ready', centerX(about) - readySize.width / 2, centerY(login))

  const or = at('or', column(3) - GROUP_PAD_X, linkedinTop - OR_GAP - size('or').height)
  const laneX = right(linkedinGroup) + LANE_OUT

  const nodes: Record<MapNode, Rect> = {
    start,
    about_you: about,
    import: imported,
    gmail,
    chrome,
    first_sync: sync,
    first_inbox: inbox,
    arm_schedule: arm,
    triage,
    build_list: list,
    campaign,
    login_item: login,
    ready,
  }
  const placement: Placement = {
    nodes,
    groups: { gmail: gmailGroup, linkedin: linkedinGroup },
    or,
    laneX,
    width: laneX + LANE_MARGIN,
    height: 0,
  }
  // Start can rise above the Gmail group when step 1 is short; shift everything down
  // so the topmost thing keeps the margin, then size the canvas to the lowest.
  const rects = [...Object.values(nodes), gmailGroup, linkedinGroup, or]
  const shift = MARGIN - Math.min(...rects.map((rect) => rect.top))
  if (shift > 0) rects.forEach((rect) => (rect.top += shift))
  placement.height = Math.max(...rects.map(bottom)) + MARGIN
  return placement
}

/** Whether the steps are the ones the map has a place for, so it can draw them. */
export function fitsMap(steps: readonly SetupStep[]): boolean {
  const keys = new Set(steps.map((step) => step.key))
  return keys.size === MAP_STEPS.length && MAP_STEPS.every((key) => keys.has(key))
}

export const right = (rect: Rect) => rect.left + rect.width
export const bottom = (rect: Rect) => rect.top + rect.height
export const centerX = (rect: Rect) => rect.left + rect.width / 2
export const centerY = (rect: Rect) => rect.top + rect.height / 2

// --- connectors ----------------------------------------------------------------------

/**
 * How a connector runs. `across`: out of the right side, into the left. `back`: out of
 * the left side, into the right, along row 2. `down`: out of the bottom, into the top.
 * `lane`: out of the right side, down past the LinkedIn group, into the right side.
 */
export type WireKind = 'across' | 'back' | 'down' | 'lane'

export interface WireDef {
  from: MapNode
  to: MapNode
  kind: WireKind
  /** One of the "one or both" pair: dotted while neither end is reached. */
  alt?: boolean
}

export const WIRES: readonly WireDef[] = [
  { from: 'start', to: 'about_you', kind: 'down' },
  { from: 'about_you', to: 'import', kind: 'across' },
  { from: 'import', to: 'gmail', kind: 'across', alt: true },
  { from: 'import', to: 'chrome', kind: 'across', alt: true },
  { from: 'chrome', to: 'first_sync', kind: 'across' },
  { from: 'chrome', to: 'first_inbox', kind: 'across' },
  { from: 'first_sync', to: 'arm_schedule', kind: 'across' },
  { from: 'first_inbox', to: 'arm_schedule', kind: 'across' },
  { from: 'gmail', to: 'triage', kind: 'lane' },
  { from: 'arm_schedule', to: 'triage', kind: 'down' },
  { from: 'triage', to: 'build_list', kind: 'back' },
  { from: 'build_list', to: 'campaign', kind: 'back' },
  { from: 'campaign', to: 'login_item', kind: 'back' },
  { from: 'login_item', to: 'ready', kind: 'back' },
]

/**
 * How far short of its box a connector ends. The arrowhead's tip is the path's end,
 * and a box's 1px ring sits just outside its rect, so the tip touches the ring.
 */
export const TIP_GAP = 1
/** The radius of each rounded corner. */
export const CORNER = 10

export type Point = readonly [number, number]

/**
 * Where the vertical part of an `across` connector runs. Connectors out of one box
 * share one stem (a fan-out), and connectors into one box share one line (a join):
 * both turn at the same x, halfway across the gutter.
 */
function trunkX(wire: WireDef, nodes: Record<MapNode, Rect>, wires: readonly WireDef[]): number {
  const across = wires.filter((other) => other.kind === 'across')
  const outs = across.filter((other) => other.from === wire.from)
  const ins = across.filter((other) => other.to === wire.to)
  const from = right(nodes[wire.from])
  const to = nodes[wire.to].left
  if (outs.length > 1)
    return Math.round((from + Math.min(...outs.map((other) => nodes[other.to].left))) / 2)
  if (ins.length > 1)
    return Math.round((Math.max(...ins.map((other) => right(nodes[other.from]))) + to) / 2)
  return Math.round((from + to) / 2)
}

/** The corner points of one connector, from its source's edge to just short of its target. */
export function routePoints(
  wire: WireDef,
  nodes: Record<MapNode, Rect>,
  laneX: number,
  wires: readonly WireDef[] = WIRES,
): Point[] {
  const a = nodes[wire.from]
  const b = nodes[wire.to]
  switch (wire.kind) {
    case 'down': {
      const middle = (bottom(a) + b.top) / 2
      return [
        [centerX(a), bottom(a)],
        [centerX(a), middle],
        [centerX(b), middle],
        [centerX(b), b.top - TIP_GAP],
      ]
    }
    case 'back': {
      const middle = Math.round((a.left + right(b)) / 2)
      return [
        [a.left, centerY(a)],
        [middle, centerY(a)],
        [middle, centerY(b)],
        [right(b) + TIP_GAP, centerY(b)],
      ]
    }
    case 'lane':
      return [
        [right(a), centerY(a)],
        [laneX, centerY(a)],
        [laneX, centerY(b)],
        [right(b) + TIP_GAP, centerY(b)],
      ]
    case 'across': {
      const x = trunkX(wire, nodes, wires)
      return [
        [right(a), centerY(a)],
        [x, centerY(a)],
        [x, centerY(b)],
        [b.left - TIP_GAP, centerY(b)],
      ]
    }
  }
}

const near = (left: number, right: number) => Math.abs(left - right) <= 0.5

/** Drops repeated points and points in the middle of a straight run. */
function simplify(points: readonly Point[]): Point[] {
  const unique: Point[] = []
  for (const point of points) {
    const last = unique.at(-1)
    if (last === undefined || !near(last[0], point[0]) || !near(last[1], point[1]))
      unique.push(point)
  }
  return unique.filter((point, index) => {
    const before = unique[index - 1]
    const after = unique[index + 1]
    if (before === undefined || after === undefined) return true
    const straight =
      (near(before[0], point[0]) && near(point[0], after[0])) ||
      (near(before[1], point[1]) && near(point[1], after[1]))
    return !straight
  })
}

const num = (value: number) => String(Math.round(value * 10) / 10)

/**
 * An SVG path through `points`, each corner rounded with radius `radius` (or less,
 * where a run is too short for it). Every run is horizontal or vertical.
 */
export function roundedPath(points: readonly Point[], radius = CORNER): string {
  const route = simplify(points)
  const first = route[0]
  if (first === undefined) return ''
  let path = `M${num(first[0])},${num(first[1])}`
  for (let index = 1; index < route.length - 1; index += 1) {
    const [x0, y0] = route[index - 1] as Point
    const [x1, y1] = route[index] as Point
    const [x2, y2] = route[index + 1] as Point
    const into = Math.hypot(x1 - x0, y1 - y0)
    const out = Math.hypot(x2 - x1, y2 - y1)
    const r = Math.min(radius, into / 2, out / 2)
    const ax = x1 - ((x1 - x0) / into) * r
    const ay = y1 - ((y1 - y0) / into) * r
    const bx = x1 + ((x2 - x1) / out) * r
    const by = y1 + ((y2 - y1) / out) * r
    path += ` L${num(ax)},${num(ay)} Q${num(x1)},${num(y1)} ${num(bx)},${num(by)}`
  }
  const last = route.at(-1) as Point
  if (route.length > 1) path += ` L${num(last[0])},${num(last[1])}`
  return path
}

export interface Wire extends WireDef {
  d: string
}

/** Every connector's path, from the placed rects. */
export function routeWires(placement: Pick<Placement, 'nodes' | 'laneX'>): Wire[] {
  return WIRES.map((wire) => ({
    ...wire,
    d: roundedPath(routePoints(wire, placement.nodes, placement.laneX)),
  }))
}

// --- looks ---------------------------------------------------------------------------

/** How a box looks, from its state. */
export type BoxLook = 'done' | 'next' | 'waiting' | 'skipped' | 'todo'

export function boxLook(step: SetupStep): BoxLook {
  if (step.state === 'done') return 'done'
  if (step.state === 'blocked') return 'waiting'
  if (step.state === 'skipped') return 'skipped'
  if (step.next || step.state === 'in_progress') return 'next'
  return 'todo'
}

/** The state in words, for the badge and for a screen reader. */
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

/** What a box's link says: Start a step, Continue one in progress, Open the rest. */
export function actionLabel(step: SetupStep): string {
  if (step.state === 'in_progress') return 'Continue'
  if (step.state === 'todo') return 'Start'
  return 'Open'
}

/**
 * What a box's skip button does, or null for none. Only a skippable step that isn't
 * done has one, and a waiting step only to undo its skip. Steps 5 to 7 that read
 * skipped because 4 is skipped have none: their skip is undone at 4.
 */
export function skipAction(step: SetupStep): 'skip' | 'undo' | null {
  if (!step.skippable || step.state === 'done') return null
  if (step.skipped) return 'undo'
  if (step.state === 'blocked' || step.state === 'skipped') return null
  return 'skip'
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

/**
 * How a connector looks: `done` (green) once both ends are done or skipped, `next`
 * (the moving accent) into the step to do now from a reached one, and otherwise
 * `alt` (dotted, the "one or both" pair) or `base` (dashed grey). Start is always
 * reached, and the connector into Ready lights once setup is complete.
 */
export type WireLook = 'done' | 'next' | 'alt' | 'base'

export function wireLook(
  wire: WireDef,
  steps: ReadonlyMap<string, SetupStep>,
  complete: boolean,
): WireLook {
  // Into Ready: lit once setup is complete, whatever the optional step 11 says.
  if (wire.to === 'ready') return complete ? 'done' : 'base'
  const reached = (key: MapNode) => {
    if (key === 'start') return true
    const step = steps.get(key)
    return step !== undefined && (step.state === 'done' || step.state === 'skipped')
  }
  if (steps.get(wire.to)?.next === true && reached(wire.from)) return 'next'
  if (reached(wire.from) && reached(wire.to)) return 'done'
  return wire.alt === true ? 'alt' : 'base'
}
