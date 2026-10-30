import { describe, expect, it } from 'vitest'

import { TONE_CHIP_CLASSES } from './tone-classes'

describe('TONE_CHIP_CLASSES', () => {
  // #752 review: the soft tints are see-through, so each chip needs the opaque `bg-card`
  // under its tint. Without it the light inks drop below AA on the field (tone-classes.ts).
  it.each(Object.entries(TONE_CHIP_CLASSES))(
    'gives the %s chip an opaque card base',
    (_, classes) => {
      expect(classes.split(/\s+/)).toContain('bg-card')
    },
  )

  it('tints every chip but off', () => {
    for (const [tone, classes] of Object.entries(TONE_CHIP_CLASSES)) {
      expect(/\bfrom-\S+-soft\b/.test(classes)).toBe(tone !== 'off')
    }
  })
})
