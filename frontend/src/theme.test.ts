/**
 * The theme in `index.css` stays readable, and the icon stays its color (#56, #606).
 *
 * jsdom has no layout and no CSS engine, so this reads the tokens straight out
 * of the stylesheet, resolves their `var()`, `calc()` and `color-mix()`, and
 * holds every pair of a text token on the surface it is drawn on to WCAG AA
 * (4.5:1) in both schemes. A glass pane is see-through, so it is composited
 * over the page and over the center of each color-field blob, its most
 * saturated spot, and the worst of those has to pass. A tint changes the ink
 * inside it (`.nk-tint`), so text in a tinted pane is checked with those inks.
 */
import { describe, expect, it } from 'vitest'

import indexHtml from '../index.html?raw'
import faviconSvg from '../public/favicon.svg?raw'

// `vite.config.ts` lets this one stylesheet through vitest's CSS stub.
import rawCss from './index.css?raw'

const css = rawCss.replace(/\/\*[\s\S]*?\*\//g, '')

type Tokens = Record<string, string>

/** The custom properties declared directly inside `block`. */
function tokensIn(block: string): Tokens {
  const tokens: Tokens = {}
  for (const match of block.matchAll(/--([a-z0-9-]+):\s*([^;]+);/g)) {
    tokens[match[1] as string] = (match[2] as string).trim()
  }
  return tokens
}

/** The body of the first `{ ... }` after `start`, matched brace for brace. */
function blockAfter(start: number): string {
  if (start < 0) throw new Error('index.css is missing a block this test reads')
  const open = css.indexOf('{', start)
  let depth = 0
  for (let index = open; index < css.length; index += 1) {
    if (css[index] === '{') depth += 1
    if (css[index] === '}') depth -= 1
    if (depth === 0) return css.slice(open + 1, index)
  }
  throw new Error('unbalanced braces in index.css')
}

const light = tokensIn(blockAfter(css.indexOf(':root {')))
const dark = {
  ...light,
  ...tokensIn(blockAfter(css.indexOf('@media (prefers-color-scheme: dark)'))),
}
/** What `.nk-tint` redefines inside a tinted pane. */
const inTint = tokensIn(blockAfter(css.indexOf('.nk-tint {')))

// ---- Color math: linear-light sRGB with straight alpha ----

interface Color {
  rgb: [number, number, number]
  alpha: number
}

const clamp = (x: number) => Math.min(1, Math.max(0, x))
const decode = (x: number) => (x <= 0.04045 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4)
const encode = (x: number) => (x <= 0.0031308 ? 12.92 * x : 1.055 * x ** (1 / 2.4) - 0.055)

function fromOklab([lightness, a, b]: number[]): [number, number, number] {
  const l =
    ((lightness as number) + 0.3963377774 * (a as number) + 0.2158037573 * (b as number)) ** 3
  const m =
    ((lightness as number) - 0.1055613458 * (a as number) - 0.0638541728 * (b as number)) ** 3
  const s =
    ((lightness as number) - 0.0894841775 * (a as number) - 1.291485548 * (b as number)) ** 3
  return [
    clamp(4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s),
    clamp(-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s),
    clamp(-0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s),
  ]
}

function toOklab([r, g, b]: [number, number, number]): [number, number, number] {
  const l = Math.cbrt(0.4122214708 * r + 0.5363288246 * g + 0.0514459244 * b)
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
  return [
    0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
    1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
  ]
}

/** `color-mix(in oklab, one p, other)`: premultiplied, as the browser mixes. */
function mix(one: Color, p: number, other: Color): Color {
  const alpha = one.alpha * p + other.alpha * (1 - p)
  if (alpha === 0) return { rgb: [0, 0, 0], alpha: 0 }
  const x = toOklab(one.rgb)
  const y = toOklab(other.rgb)
  const lab = x.map(
    (v, i) => (v * one.alpha * p + (y[i] as number) * other.alpha * (1 - p)) / alpha,
  )
  return { rgb: fromOklab(lab), alpha }
}

/** `top` painted over an opaque `bottom`: browsers blend in gamma-encoded sRGB. */
function over(top: Color, bottom: Color): Color {
  const rgb = top.rgb.map((channel, i) =>
    decode(encode(channel) * top.alpha + encode(bottom.rgb[i] as number) * (1 - top.alpha)),
  ) as [number, number, number]
  return { rgb, alpha: 1 }
}

function luminance({ rgb: [r, g, b] }: Color): number {
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

function contrast(text: Color, surface: Color): number {
  if (surface.alpha < 1) throw new Error('a surface has to be opaque to measure against')
  const one = luminance(text.alpha < 1 ? over(text, surface) : text)
  const other = luminance(surface)
  return (Math.max(one, other) + 0.05) / (Math.min(one, other) + 0.05)
}

// ---- Reading a token's value ----

/** The comma-separated arguments of a function call, split at the top level only. */
function args(inner: string): string[] {
  const parts: string[] = []
  let depth = 0
  let start = 0
  for (let index = 0; index < inner.length; index += 1) {
    if (inner[index] === '(') depth += 1
    if (inner[index] === ')') depth -= 1
    if (inner[index] === ',' && depth === 0) {
      parts.push(inner.slice(start, index).trim())
      start = index + 1
    }
  }
  parts.push(inner.slice(start).trim())
  return parts
}

/** Substitute every `var(--x)` from `tokens`, then fold simple `calc(a * b)`. */
function resolve(value: string, tokens: Tokens, depth = 0): string {
  if (depth > 20) throw new Error(`var() cycle in ${value}`)
  const substituted = value.replace(/var\(--([a-z0-9-]+)\)/g, (_, name: string) => {
    const token = tokens[name]
    if (token === undefined) throw new Error(`index.css has no --${name}`)
    return resolve(token, tokens, depth + 1)
  })
  return substituted.replace(
    /calc\(([\d.]+)(%?) \* ([\d.]+)\)/g,
    (_, a: string, unit: string, b: string) => {
      return `${Number(a) * Number(b)}${unit}`
    },
  )
}

const number = (part: string) =>
  part.endsWith('%') ? Number(part.slice(0, -1)) / 100 : Number(part)

function parse(value: string): Color {
  const text = value.trim()
  if (text === 'transparent') return { rgb: [0, 0, 0], alpha: 0 }
  const hex = /^#([0-9a-f]{6})$/i.exec(text)
  if (hex) {
    const n = parseInt(hex[1] as string, 16)
    return {
      rgb: [(n >> 16) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255].map(decode) as Color['rgb'],
      alpha: 1,
    }
  }
  const rgb = /^rgb\(([\d.]+) ([\d.]+) ([\d.]+)(?: \/ ([\d.]+%?))?\)$/.exec(text)
  if (rgb) {
    const channels = rgb.slice(1, 4).map((c) => decode(Number(c) / 255)) as Color['rgb']
    return { rgb: channels, alpha: rgb[4] === undefined ? 1 : number(rgb[4]) }
  }
  const oklch = /^oklch\(([\d.]+%?) ([\d.]+) ([\d.]+)(?: \/ ([\d.]+%?))?\)$/.exec(text)
  if (oklch) {
    const [lightness, chroma, hue] = [
      number(oklch[1] as string),
      Number(oklch[2]),
      Number(oklch[3]),
    ]
    const a = chroma * Math.cos((hue * Math.PI) / 180)
    const b = chroma * Math.sin((hue * Math.PI) / 180)
    return {
      rgb: fromOklab([lightness, a, b]),
      alpha: oklch[4] === undefined ? 1 : number(oklch[4]),
    }
  }
  const colorMix = /^color-mix\(in oklab, ([\s\S]+)\)$/.exec(text)
  if (colorMix) {
    const [first, second] = args(colorMix[1] as string) as [string, string]
    const weighted = /^([\s\S]+) ([\d.]+)%$/.exec(first)
    if (!weighted) throw new Error(`color-mix() needs a weight on its first color: ${text}`)
    return mix(parse(weighted[1] as string), Number(weighted[2]) / 100, parse(second))
  }
  throw new Error(`not a color this test reads: ${text}`)
}

const colorOf = (tokens: Tokens, name: string) => parse(resolve(`var(--${name})`, tokens))
const percentOf = (tokens: Tokens, name: string) => number(resolve(`var(--${name})`, tokens))

/**
 * Custom properties as the browser computes them on an element inside `.nk-tint`.
 * Every `:root` token arrives already computed (inherited), so an alias such as
 * `--destructive: var(--bad-ink)` keeps the `:root` value of `--bad-ink`; only what
 * `.nk-tint` itself declares is recomputed there, against those inherited values.
 */
function insideTint(tokens: Tokens): Tokens {
  const inherited: Tokens = {}
  for (const [name, value] of Object.entries(tokens)) {
    try {
      inherited[name] = resolve(value, tokens)
    } catch {
      inherited[name] = value
    }
  }
  return { ...inherited, ...inTint }
}

/** `:root` color tokens that are aliases of a token `.nk-tint` redefines. */
function aliasesOfTinted(tokens: Tokens): string[] {
  const tinted = new Set(Object.keys(inTint))
  return Object.entries(tokens)
    .filter(([name, value]) =>
      [...value.matchAll(/var\(--([a-z0-9-]+)\)/g)].some(
        (match) => tinted.has(match[1] as string) && !tinted.has(name),
      ),
    )
    .map(([name]) => name)
}

// ---- The surfaces ----

const TONES = { accent: 'brand', ok: 'ok', warn: 'warn', bad: 'bad' } as const
const INKS = ['ok-ink', 'warn-ink', 'bad-ink', 'accent-ink'] as const
/** Text tokens that alias an ink; `.nk-tint` must redefine them too, or they skip its inks. */
const ALIASES = ['destructive'] as const
const SOFTS = {
  'ok-ink': 'ok-soft',
  'warn-ink': 'warn-soft',
  'bad-ink': 'bad-soft',
  'accent-ink': 'accent-soft',
}

/** The page, and the page under the center of each color-field blob. */
function backdrops(tokens: Tokens): Color[] {
  const page = colorOf(tokens, 'background')
  const strength = percentOf(tokens, 'field-mix') * Number(resolve('var(--field-opacity)', tokens))
  const blobs = [1, 2, 3, 4].map((n) => {
    const blob = colorOf(tokens, `field-${n}`)
    return over({ rgb: blob.rgb, alpha: strength }, page)
  })
  return [page, ...blobs]
}

/** Every opaque surface a pane can end up as: glass over each backdrop, or solid. */
function panes(tokens: Tokens, fill: Color, solid: Color): Color[] {
  return [...backdrops(tokens).map((backdrop) => over(fill, backdrop)), solid]
}

function tintPanes(tokens: Tokens, tone: keyof typeof TONES, mixToken: string): Color[] {
  const color = colorOf(tokens, TONES[tone])
  const amount = percentOf(tokens, mixToken)
  const glass = mix(color, amount, colorOf(tokens, 'glass-fill'))
  const solid = over(mix(color, amount * 0.6, colorOf(tokens, 'card')), colorOf(tokens, 'card'))
  return panes(tokens, glass, solid)
}

const worst = (text: Color, surfaces: Color[]) =>
  Math.min(...surfaces.map((s) => contrast(text, s)))

/** Every text token, on each opaque surface the kit draws it on. */
const SOLID_PAIRS: [text: string, surface: string][] = [
  ['foreground', 'background'],
  ['card-foreground', 'card'],
  ['popover-foreground', 'popover'],
  ['primary-foreground', 'primary'],
  ['secondary-foreground', 'secondary'],
  ['accent-foreground', 'accent'],
  ['muted-foreground', 'background'],
  ['muted-foreground', 'card'],
  ['muted-foreground', 'muted'],
  ['muted-foreground', 'popover'],
  ['destructive', 'background'],
  ['destructive', 'card'],
  // Links and text in the accent use its ink; the accent itself is for fills.
  ['accent-ink', 'accent'],
  ['sidebar-foreground', 'sidebar'],
  ['sidebar-accent-foreground', 'sidebar-accent'],
  ['sidebar-primary-foreground', 'sidebar-primary'],
  ...INKS.flatMap((ink) => [
    [ink, 'card'] as [string, string],
    [ink, 'background'] as [string, string],
    [ink, 'popover'] as [string, string],
  ]),
]

describe.each([
  ['light', light],
  ['dark', dark],
] as const)('the %s theme', (_, tokens) => {
  const withTint = insideTint(tokens)

  it.each(SOLID_PAIRS)('keeps %s on %s at WCAG AA', (text, surface) => {
    expect(contrast(colorOf(tokens, text), colorOf(tokens, surface))).toBeGreaterThanOrEqual(4.5)
  })

  it.each(INKS)('keeps %s on its soft tint at WCAG AA', (ink) => {
    const soft = colorOf(tokens, SOFTS[ink])
    const surfaces = [
      ...panes(tokens, colorOf(tokens, 'glass-fill'), colorOf(tokens, 'card')),
      colorOf(tokens, 'popover'),
    ].map((surface) => over(soft, surface))
    expect(worst(colorOf(tokens, ink), surfaces)).toBeGreaterThanOrEqual(4.5)
  })

  it.each(['foreground', 'muted-foreground'])(
    'keeps %s on the bare color field at WCAG AA',
    (text) => {
      expect(worst(colorOf(tokens, text), backdrops(tokens))).toBeGreaterThanOrEqual(4.5)
    },
  )

  it.each(['foreground', 'muted-foreground', 'destructive', ...INKS])(
    'keeps %s on a glass pane at WCAG AA',
    (text) => {
      const surfaces = panes(tokens, colorOf(tokens, 'glass-fill'), colorOf(tokens, 'card'))
      expect(worst(colorOf(tokens, text), surfaces)).toBeGreaterThanOrEqual(4.5)
    },
  )

  // WCAG 1.4.11: the focus ring is non-text, so 3:1 against everything it sits on.
  it('keeps the focus ring at 3:1 on the field, on glass and on every tint', () => {
    const surfaces = [
      ...backdrops(tokens),
      ...panes(tokens, colorOf(tokens, 'glass-fill'), colorOf(tokens, 'card')),
      ...(Object.keys(TONES) as (keyof typeof TONES)[]).flatMap((tone) => [
        ...tintPanes(tokens, tone, 'tint-mix'),
        ...tintPanes(tokens, tone, 'tray-mix'),
      ]),
    ]
    expect(worst(colorOf(tokens, 'ring'), surfaces)).toBeGreaterThanOrEqual(3)
  })

  describe.each(Object.keys(TONES) as (keyof typeof TONES)[])('a %s-tinted pane', (tone) => {
    const surfaces = [
      ...tintPanes(tokens, tone, 'tint-mix'),
      ...tintPanes(tokens, tone, 'tray-mix'),
    ]
    const heading = mix(colorOf(tokens, TONES[tone]), 0.45, colorOf(tokens, 'foreground'))

    it.each(['foreground', 'muted-foreground', ...INKS, ...ALIASES])(
      'keeps %s readable on it',
      (text) => {
        expect(worst(colorOf(withTint, text), surfaces)).toBeGreaterThanOrEqual(4.5)
      },
    )

    // An error or a destructive badge or button in a tinted pane: `text-destructive` on
    // `bg-bad-soft`, at rest and at the button's `/70` hover.
    it.each([1, 0.7])('keeps destructive readable on bad-soft at %s inside it', (strength) => {
      const soft = colorOf(withTint, 'bad-soft')
      const onSoft = surfaces.map((surface) =>
        over({ rgb: soft.rgb, alpha: soft.alpha * strength }, surface),
      )
      expect(worst(colorOf(withTint, 'destructive'), onSoft)).toBeGreaterThanOrEqual(4.5)
    })

    it.each(INKS)('keeps %s readable on its soft tint inside it', (ink) => {
      const soft = colorOf(withTint, SOFTS[ink])
      const onSoft = surfaces.map((surface) => over(soft, surface))
      expect(worst(colorOf(withTint, ink), onSoft)).toBeGreaterThanOrEqual(4.5)
    })

    it('keeps its heading readable on it', () => {
      expect(worst(heading, surfaces)).toBeGreaterThanOrEqual(4.5)
    })
  })
})

describe.each([
  ['light', light],
  ['dark', dark],
] as const)('a tint in the %s theme', (_, tokens) => {
  it('redefines every alias of a token it changes, so none keeps the page value', () => {
    expect(aliasesOfTinted(tokens)).toEqual([])
  })
})

/** Every source file the app is built from, as text. */
const SOURCES = import.meta.glob<string>(['./**/*.{ts,tsx}', '!./**/*.test.{ts,tsx}'], {
  query: '?raw',
  import: 'default',
  eager: true,
})

describe('red behind text', () => {
  // `bg-destructive/N` overlays red on whatever is under it. On a dark tint that
  // brightens the surface until red text drops to about 3.2:1, while `bg-bad-soft`
  // turns into plain inner glass inside a tint, which the checks above cover.
  it('is bg-bad-soft everywhere, never bg-destructive/N', () => {
    expect(Object.keys(SOURCES).length).toBeGreaterThan(100)
    const offenders = Object.keys(SOURCES).filter((file) =>
      /\bbg-destructive\/\d+/.test(SOURCES[file] as string),
    )
    expect(offenders).toEqual([])
  })
})

describe('the accent', () => {
  it('is one hue and one chroma, shared by both schemes', () => {
    expect(light['accent-hue']).toMatch(/^\d+$/)
    expect(light['accent-chroma']).toMatch(/^0\.\d+$/)
    expect(blockAfter(css.indexOf('@media (prefers-color-scheme: dark)'))).not.toMatch(
      /--accent-(hue|chroma):/,
    )
    expect(light.brand).toBe(
      'oklch(var(--accent-lightness) var(--accent-chroma) var(--accent-hue))',
    )
    expect(light.primary).toBe('var(--brand)')
  })
})

function hex(color: Color): string {
  const channels = color.rgb.map((channel) =>
    Math.round(255 * encode(channel))
      .toString(16)
      .padStart(2, '0'),
  )
  return `#${channels.join('')}`
}

describe('the app icon', () => {
  it('is drawn in the theme’s primary', () => {
    const fill = /<rect[^>]*fill="(#[0-9a-f]{6})"/.exec(faviconSvg)?.[1]
    expect(fill).toBe(hex(colorOf(light, 'primary')))
  })

  it('is linked from index.html as an SVG', () => {
    expect(indexHtml).toContain('<link rel="icon" type="image/svg+xml" href="/favicon.svg" />')
  })
})
