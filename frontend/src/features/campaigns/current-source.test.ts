import { describe, expect, it } from 'vitest'

import { currentSource } from './audience-source'
import type { Campaign } from './api'

const fromList = { source_list_id: 2, filter: null } as unknown as Campaign

describe('currentSource', () => {
  it('shows a placeholder, not the list id, while the lists load', () => {
    expect(currentSource(fromList, undefined, true)).toBe('the list …')
  })

  it('names the list once it is known', () => {
    expect(currentSource(fromList, 'Quorbelle Fictionary', false)).toBe(
      'the list Quorbelle Fictionary',
    )
  })

  it('falls back to the id when the loaded lists do not include it', () => {
    expect(currentSource(fromList, undefined, false)).toBe('the list #2')
  })
})
