/**
 * The metric tiles' pure parts (#740): the window switch and its memory, the tiles'
 * definitions, the date range in words, and each sparkline's points.
 */
import type { DashboardMetrics, MetricsBucket, MetricsWindow } from './api'

export const WINDOWS: readonly { value: MetricsWindow; label: string; heading: string }[] = [
  { value: '7d', label: '7 days', heading: 'Last 7 days' },
  { value: '30d', label: '30 days', heading: 'Last 30 days' },
  { value: '1y', label: '1 year', heading: 'Last 12 months' },
]

/** Per viewer, per browser: which window the tiles show. Nothing reaches the server. */
export const WINDOW_KEY = 'netkeep.dashboard.metricsWindow'
const DEFAULT_WINDOW: MetricsWindow = '7d'

/** The window this viewer picked last, or 7 days when storage is empty, blocked or odd. */
export function readWindow(): MetricsWindow {
  try {
    const stored = window.localStorage.getItem(WINDOW_KEY)
    return WINDOWS.find((choice) => choice.value === stored)?.value ?? DEFAULT_WINDOW
  } catch {
    // A private window can throw on the read itself.
    return DEFAULT_WINDOW
  }
}

export function rememberWindow(value: MetricsWindow): void {
  try {
    window.localStorage.setItem(WINDOW_KEY, value)
  } catch {
    // The switch still works; it just starts at 7 days next time.
  }
}

export interface TileDef {
  key: 'sent' | 'replies' | 'bounced' | 'changed_jobs' | 'new_contacts'
  label: string
  total: (metrics: DashboardMetrics) => number
  /** A small suffix after the number, such as the reply rate. */
  suffix?: (metrics: DashboardMetrics) => string | null
  /** What the suffix is, for a screen reader. */
  suffixName?: string
  /** A line under the sparkline. */
  detail?: (metrics: DashboardMetrics) => string
  point: (bucket: MetricsBucket) => number
}

export const TILES: readonly TileDef[] = [
  { key: 'sent', label: 'Sent', total: (m) => m.sent, point: (b) => b.sent },
  {
    key: 'replies',
    label: 'Replies',
    total: (m) => m.replies,
    suffix: (m) => percent(m.reply_rate),
    suffixName: 'reply rate',
    point: (b) => b.replies,
  },
  { key: 'bounced', label: 'Bounced', total: (m) => m.bounced, point: (b) => b.bounced },
  {
    key: 'changed_jobs',
    label: 'Changed jobs',
    total: (m) => m.changed_jobs,
    point: (b) => b.changed_jobs,
  },
  {
    key: 'new_contacts',
    label: 'New contacts',
    total: (m) => m.new_contacts.total,
    detail: (m) =>
      `${m.new_contacts.linkedin.toLocaleString()} LinkedIn · ${m.new_contacts.imported.toLocaleString()} imported`,
    point: (b) => b.new_contacts,
  },
]

/** "22%", or null when nothing was sent. A reply can answer an older send, so it can pass 100%. */
export function percent(rate: number | null): string | null {
  return rate === null ? null : `${Math.round(rate * 100)}%`
}

/** Nothing happened in the window: every total is 0. */
export function isEmpty(metrics: DashboardMetrics): boolean {
  return TILES.every((tile) => tile.total(metrics) === 0)
}

/** A `YYYY-MM-DD` as a local date, so no time zone moves it a day. */
function localDate(value: string): Date {
  const [year = 0, month = 1, day = 1] = value.split('-').map(Number)
  return new Date(year, month - 1, day)
}

/**
 * The window's dates in words: "Oct 14–20" or "Sep 21–Oct 20" for days, "Nov 2025–Oct
 * 2026" for months, in the reader's locale.
 */
export function rangeText(start: string, end: string, period: MetricsWindow): string {
  const from = localDate(start)
  const to = localDate(end)
  if (period === '1y') {
    const month = (date: Date) =>
      date.toLocaleDateString(undefined, { month: 'short', year: 'numeric' })
    return `${month(from)}–${month(to)}`
  }
  const day = (date: Date) => date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
  const sameMonth = from.getMonth() === to.getMonth() && from.getFullYear() === to.getFullYear()
  return sameMonth ? `${day(from)}–${to.getDate()}` : `${day(from)}–${day(to)}`
}

export interface Spark {
  /** The polyline's points, in the viewBox. */
  line: string
  /** The filled area under it, down to the baseline. */
  area: string
  /** The last point, as fractions of the box, for the endpoint dot. */
  end: { x: number; y: number }
}

export const SPARK_WIDTH = 150
export const SPARK_HEIGHT = 30

/**
 * A sparkline's points across `width` by `height`. The tallest value reaches 5 from the
 * top and 0 sits 3 above the bottom, so a flat line still shows. Null for no values.
 */
export function sparkPoints(
  values: readonly number[],
  width = SPARK_WIDTH,
  height = SPARK_HEIGHT,
): Spark | null {
  if (values.length === 0) return null
  const max = Math.max(1, ...values)
  const step = values.length === 1 ? 0 : width / (values.length - 1)
  const points = values.map((value, index): [number, number] => [
    values.length === 1 ? width : index * step,
    height - 3 - (value / max) * (height - 8),
  ])
  const text = (point: readonly [number, number]) =>
    `${Math.round(point[0] * 10) / 10},${Math.round(point[1] * 10) / 10}`
  const line = points.map(text).join(' ')
  const base = height - 1
  const first = points[0] as [number, number]
  const last = points.at(-1) as [number, number]
  return {
    line,
    area: `${text([first[0], base])} ${line} ${text([last[0], base])}`,
    end: { x: last[0] / width, y: last[1] / height },
  }
}
