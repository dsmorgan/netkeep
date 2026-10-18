/**
 * State tones for surfaces that need you (#606, direction B5 "Tinted glass").
 *
 * A toned pane is tinted with its state color and carries two offset sheets
 * beneath it; `index.css` draws both from the `nk-tint` and `nk-tone-*`
 * classes. The accent is the live run and the send tray, amber a warning,
 * green a reply, and red something that stopped.
 */
export type Tone = 'accent' | 'ok' | 'warn' | 'bad'

/** The classes that tint a pane with `tone`; nothing when there is no tone. */
export function toneClasses(tone: Tone | undefined, { tray = false }: { tray?: boolean } = {}) {
  if (tone === undefined) return ''
  // The send tray holds a list to read through, so its tint is lighter.
  return tray ? `nk-tint nk-tone-${tone} nk-tray` : `nk-tint nk-tone-${tone}`
}
