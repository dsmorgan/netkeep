import { describe, expect, it } from 'vitest'

import { toneClasses } from './tone'

describe('toneClasses', () => {
  it.each([
    ['accent', 'nk-tint nk-tone-accent'],
    ['ok', 'nk-tint nk-tone-ok'],
    ['warn', 'nk-tint nk-tone-warn'],
    ['bad', 'nk-tint nk-tone-bad'],
  ] as const)('tints a %s pane with its own state color', (tone, classes) => {
    expect(toneClasses(tone)).toBe(classes)
  })

  it('lightens the tint for the send tray', () => {
    expect(toneClasses('accent', { tray: true })).toBe('nk-tint nk-tone-accent nk-tray')
  })

  it('leaves a pane with no tone plain', () => {
    expect(toneClasses(undefined)).toBe('')
    expect(toneClasses(undefined, { tray: true })).toBe('')
  })
})
