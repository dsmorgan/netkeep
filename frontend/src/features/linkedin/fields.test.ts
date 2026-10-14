import { describe, expect, it } from 'vitest'

import { formatFields, formatWhen, summarizeFields } from './fields'

describe('formatWhen', () => {
  it('renders an em dash for null', () => {
    expect(formatWhen(null)).toBe('—')
  })

  it('renders an unparseable string as-is', () => {
    expect(formatWhen('not a date')).toBe('not a date')
  })

  it('renders a real timestamp in the reader’s locale', () => {
    expect(formatWhen('2026-09-20T10:00:00Z')).not.toBe('2026-09-20T10:00:00Z')
    expect(formatWhen('2026-09-20T10:00:00Z')).toContain('2026')
  })
})

describe('formatFields', () => {
  it('is empty for null or undefined', () => {
    expect(formatFields(null)).toEqual([])
    expect(formatFields(undefined)).toEqual([])
  })

  it('orders known enrichment progress keys and title-cases their labels', () => {
    const fields = formatFields({ planned: 10, visited: 3, harvested: 2, not_found: 1 })
    expect(fields.map((f) => f.label)).toEqual(['Planned', 'Visited', 'Harvested', 'Not Found'])
    expect(fields.map((f) => f.value)).toEqual(['10', '3', '2', '1'])
  })

  it('orders known connections-sync progress keys', () => {
    const fields = formatFields({ mode: 'incremental', pages: 2, connections: 80, total: 400 })
    expect(fields.map((f) => f.label)).toEqual(['Mode', 'Pages', 'Connections', 'Total'])
  })

  it('shows a sync’s hidden members right after its total (#515)', () => {
    const fields = formatFields({ shortfall: 3, hidden: 3, total: 626, connections: 623 })
    expect(fields.map((f) => f.label)).toEqual(['Connections', 'Total', 'Hidden', 'Shortfall'])
    expect(fields.map((f) => f.value)).toEqual(['623', '626', '3', '3'])
  })

  it('shows the rescued copies not found whole right after the hidden members (#525)', () => {
    const fields = formatFields({ zeta: 1, rescued_not_whole: 0, hidden: 3, total: 46 })
    expect(fields.map((f) => f.label)).toEqual(['Total', 'Hidden', 'Rescued Not Whole', 'Zeta'])
    expect(fields.map((f) => f.value)).toEqual(['46', '3', '0', '1'])
  })

  it('shows a run’s snapshots and field changes after its other counts (#518)', () => {
    const fields = formatFields({
      field_changes: { headline: 3, title: 0, company: 0, location: 1 },
      zeta: 1,
      snapshots: 4,
      total: 46,
    })
    expect(fields.map((f) => f.label)).toEqual(['Total', 'Snapshots', 'Field Changes', 'Zeta'])
    expect(fields[2]?.value).toContain('"headline":3')
  })

  it('appends unknown keys, alphabetically, after the known ones', () => {
    const fields = formatFields({ visited: 1, zeta: 'z', alpha: 'a' })
    expect(fields.map((f) => f.label)).toEqual(['Visited', 'Alpha', 'Zeta'])
  })

  it('renders null, booleans, and objects readably', () => {
    const fields = formatFields({ stopped: null, cancelled: true, plan: { a: 1 } })
    const byLabel = Object.fromEntries(fields.map((f) => [f.label, f.value]))
    expect(byLabel.Stopped).toBe('—')
    expect(byLabel.Cancelled).toBe('yes')
    expect(byLabel['Plan A']).toBe('1')
  })

  it('renders a nested count object as labeled rows, not JSON (#465)', () => {
    const fields = formatFields({
      planned: 0,
      harvests: { applied: 0, not_found: 2, nested: { deep_count: 4 } },
      empty: {},
      list: [1, 2],
    })
    expect(fields).toEqual([
      { label: 'Planned', value: '0' },
      { label: 'Empty', value: '—' },
      { label: 'Harvests Not Found', value: '2' },
      { label: 'Harvests Applied', value: '0' },
      { label: 'Harvests Nested Deep Count', value: '4' },
      { label: 'List', value: '[1,2]' },
    ])
  })
})

describe('summarizeFields', () => {
  it('is an em dash when there is nothing to show', () => {
    expect(summarizeFields(null)).toBe('—')
  })

  it('joins the first few fields onto one line', () => {
    expect(summarizeFields({ planned: 10, visited: 3, harvested: 2, not_found: 1 })).toBe(
      'planned: 10, visited: 3, harvested: 2',
    )
  })
})
