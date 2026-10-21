import { afterEach, describe, expect, it, vi } from 'vitest'

import type { DashboardMetrics } from './api'
import {
  WINDOW_KEY,
  isEmpty,
  percent,
  rangeText,
  readWindow,
  rememberWindow,
  sparkPoints,
} from './metrics'

afterEach(() => {
  vi.restoreAllMocks()
  window.localStorage.clear()
})

describe('the window switch’s memory', () => {
  it('reads back what was remembered, and 7 days for nothing or something odd', () => {
    expect(readWindow()).toBe('7d')
    rememberWindow('1y')
    expect(window.localStorage.getItem(WINDOW_KEY)).toBe('1y')
    expect(readWindow()).toBe('1y')
    window.localStorage.setItem(WINDOW_KEY, 'forever')
    expect(readWindow()).toBe('7d')
  })

  it('survives storage that throws', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    expect(readWindow()).toBe('7d')
    expect(() => rememberWindow('30d')).not.toThrow()
  })
})

describe('rangeText', () => {
  it('names days within a month, across months, and months across years', () => {
    expect(rangeText('2026-10-14', '2026-10-20', '7d')).toBe('Oct 14–20')
    expect(rangeText('2026-09-21', '2026-10-20', '30d')).toBe('Sep 21–Oct 20')
    expect(rangeText('2025-11-01', '2026-10-20', '1y')).toBe('Nov 2025–Oct 2026')
  })
})

describe('percent', () => {
  it('rounds the reply rate, and says nothing when nothing was sent', () => {
    expect(percent(0.2222)).toBe('22%')
    expect(percent(1.5)).toBe('150%')
    expect(percent(null)).toBeNull()
  })
})

describe('isEmpty', () => {
  const metrics = (overrides: Partial<DashboardMetrics> = {}): DashboardMetrics => ({
    window: '7d',
    start: '2026-10-14',
    end: '2026-10-20',
    sent: 0,
    replies: 0,
    reply_rate: null,
    bounced: 0,
    changed_jobs: 0,
    new_contacts: { total: 0, linkedin: 0, imported: 0 },
    series: [],
    ...overrides,
  })

  it('is empty only when every total is 0', () => {
    expect(isEmpty(metrics())).toBe(true)
    expect(isEmpty(metrics({ changed_jobs: 1 }))).toBe(false)
    expect(isEmpty(metrics({ new_contacts: { total: 2, linkedin: 2, imported: 0 } }))).toBe(false)
  })
})

describe('sparkPoints', () => {
  it('spreads the values across the width, the tallest at the top', () => {
    const spark = sparkPoints([0, 2, 1], 100, 30)
    expect(spark?.line).toBe('0,27 50,5 100,16')
    // The area closes along the baseline, under the first and last points.
    expect(spark?.area).toBe('0,29 0,27 50,5 100,16 100,29')
    expect(spark?.end).toEqual({ x: 1, y: 16 / 30 })
  })

  it('keeps a flat line of zeros on the baseline, and handles one value or none', () => {
    expect(sparkPoints([0, 0], 100, 30)?.line).toBe('0,27 100,27')
    expect(sparkPoints([4], 100, 30)?.line).toBe('100,5')
    expect(sparkPoints([])).toBeNull()
  })
})
