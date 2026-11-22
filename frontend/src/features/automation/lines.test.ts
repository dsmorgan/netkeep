import { describe, expect, it } from 'vitest'

import { automaticUnavailable } from './lines'

describe('automaticUnavailable (#835)', () => {
  it.each([
    [null, 'Automatic isn’t available yet.'],
    ['', 'Automatic isn’t available yet.'],
    ['   ', 'Automatic isn’t available yet.'],
    ['Connect a Gmail mailbox first', 'Automatic isn’t available: Connect a Gmail mailbox first.'],
    [
      'The mailbox needs signing in again.',
      'Automatic isn’t available: The mailbox needs signing in again.',
    ],
  ])('words %j as one sentence with one prefix', (reason, text) => {
    expect(automaticUnavailable(reason)).toBe(text)
  })
})
