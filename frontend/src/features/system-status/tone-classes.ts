import type { Tone } from '@/components/ui/tone'

import type { StatusTone } from './roll-up'

/** The B5 state tone (`components/ui/tone.ts`) each status tone borrows; busy is the accent. */
export const SURFACE_TONE: Record<Exclude<StatusTone, 'off'>, Tone> = {
  ok: 'ok',
  busy: 'accent',
  warn: 'warn',
  bad: 'bad',
}

/**
 * The soft tint is a gradient layer over an opaque card, so the color field never shows
 * through: on the bare field a blob drops the light inks to about 3.5:1 (theme.test.ts).
 */
const TONE_CLASSES: Record<Tone, string> = {
  ok: 'border-ok/40 bg-card bg-linear-to-r from-ok-soft to-ok-soft text-ok-ink',
  warn: 'border-warn/45 bg-card bg-linear-to-r from-warn-soft to-warn-soft text-warn-ink',
  bad: 'border-bad/45 bg-card bg-linear-to-r from-bad-soft to-bad-soft text-bad-ink',
  accent:
    'border-primary/45 bg-card bg-linear-to-r from-accent-soft to-accent-soft text-accent-ink',
}

/** A status chip's border, tint and ink for each tone, from the soft tokens; off has none. */
export const TONE_CHIP_CLASSES: Record<StatusTone, string> = {
  ok: TONE_CLASSES[SURFACE_TONE.ok],
  busy: TONE_CLASSES[SURFACE_TONE.busy],
  warn: TONE_CLASSES[SURFACE_TONE.warn],
  bad: TONE_CLASSES[SURFACE_TONE.bad],
  off: 'border-border bg-card text-muted-foreground',
}
