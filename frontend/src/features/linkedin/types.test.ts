import { describe, expect, it } from 'vitest'

import { runKindLabel } from './types'

describe('runKindLabel (#704)', () => {
  it('calls a scheduled message_send an auto-send and a manual one a prefill', () => {
    expect(runKindLabel({ kind: 'message_send', trigger: 'scheduled' })).toBe('LinkedIn auto-send')
    expect(runKindLabel({ kind: 'message_send', trigger: 'manual' })).toBe('LinkedIn prefill')
  })

  it('leaves every other kind alone, whoever started it', () => {
    expect(runKindLabel({ kind: 'inbox', trigger: 'scheduled' })).toBe('Inbox poll')
    expect(runKindLabel({ kind: 'enrich', trigger: 'manual' })).toBe('Enrichment')
  })
})
