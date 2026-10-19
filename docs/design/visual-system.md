# Visual system

The web UI follows direction **B5, Tinted glass**, chosen on #605 (2026-10-18): a navy sidebar, heavy headings and strong state colors, with every card a glass pane over a subtle color field. Cards that need you are tinted with their state color and carry two offset sheets underneath for depth.

Every token lives in `frontend/src/index.css`: light values in `:root`, dark overrides in the `dark` variant nested inside it (`@variant dark`).

## Theme switch

The sidebar footer has an Auto, Light and Dark switch (#666). It's in the sidebar, not the top bar, because the top bar is full of live status, and a sticky footer keeps it on screen on every page. Auto is the default and follows the OS. The choice is saved per browser in `localStorage` (`netkeeper.theme`).

- Light and Dark set `data-theme` on `<html>`; Auto removes it. `@custom-variant dark` in `index.css` matches `[data-theme="dark"]`, or `prefers-color-scheme: dark` without `[data-theme="light"]`. The dark tokens, `color-scheme` and Tailwind's `dark:` utilities all use that one variant.
- An inline script in `index.html` applies the saved choice before the first paint, and also sets the `color-scheme` meta, so the page never flashes the other theme. `src/lib/theme.ts` does the same when you switch.
- `theme.test.ts` runs the contrast checks for Auto on each OS scheme and for each forced theme against the OS.

## Tokens

| Group | Tokens | Notes |
| --- | --- | --- |
| Accent | `--accent-hue` (255), `--accent-chroma` (0.2), `--accent-lightness` (55% light, 74% dark), `--brand` | `--primary`, the focus ring, the charts, the sidebar badge and the first color-field blob all derive from the hue. |
| Neutrals | `--background`, `--foreground`, `--card`, `--popover`, `--muted`, `--muted-foreground`, `--secondary`, `--border`, `--input` | shadcn's names, so the kit picks them up. |
| Sidebar | `--sidebar`, `--sidebar-foreground`, `--sidebar-accent` (the active link), `--sidebar-primary` | Navy in both schemes. |
| State | `--ok`, `--warn`, `--bad`, each with `-ink` (text) and `-soft` (a 14 to 16% tint behind it); `--accent-ink`, `--accent-soft` | Tailwind utilities: `text-warn-ink`, `bg-warn-soft`, `border-warn/40`, `bg-ok`. `--destructive` is `--bad-ink`. |
| Type | `text-page-title`, `text-card-title`, `text-eyebrow`, `tracking-heading` | System font stacks. Headings are bold (cards, dialogs) or extra bold (page title). |
| Spacing | `--space-page`, `--space-card`, `--space-card-sm` | |
| Radius | `--radius` (12px), `rounded-control` (0.6 of it, for buttons and inputs) | Cards use `--radius`. |
| Elevation | `--elevation-pane`, `--elevation-overlay`, `--elevation-drop` | `shadow-overlay` is for dialogs, popovers and menus. |
| Glass | `--glass`, `--glass-opacity` (62%), `--glass-blur` (18px), `--glass-saturate`, `--glass-edge`, `--glass-highlight`, `--glass-rim`, `--glass-fill`, `--tint-mix` (20% light, 30% dark), `--tray-mix`, `--tint-ink-mix`, `--stack-near`, `--stack-far` (the two sheets, stronger in dark) | |
| Field | `--field-opacity` (0.45, "Subtle"), `--field-mix`, `--field-1` to `--field-4` | Four soft radial-gradient blobs fixed behind the shell. |

## Materials

- **`nk-glass`** is a pane. `Card` has it by default, and so do the shell banners and the triage panes.
- **`nk-tint nk-tone-<tone>`** tints a pane. Pass `tone` to `Card` (`accent`, `ok`, `warn` or `bad`), or call `toneClasses(tone)` from `components/ui/tone.ts` for a surface that is not a `Card`. `tray` lightens the tint for a list to read through, such as Waiting for you.
- Inside a tint, the heading and the `-ink` colors (and `--destructive`, which aliases `--bad-ink` and so is restated there) mix 45% of the state color into the ink, muted text leans 45% toward the ink, and the `-soft` tints become inner glass. Use `bg-bad-soft` behind red text, never `bg-destructive/N`; a test enforces it.

Tone only what needs the person: the live run (accent, amber while paused), the send tray, replies (ok), a stopped session or mailbox (bad).

## Fallbacks and accessibility

- Panes are solid first. Glass is applied only inside `@supports (backdrop-filter: blur(1px))`, and `prefers-reduced-transparency: reduce` (macOS's Reduce transparency) makes the panes solid again. The field stays on: it's the page background, and page text is held to AA on it. A solid tint is the state color at 0.6 of the tint mix over `--card`.
- Reduced motion stops the field's drift.
- Focus is a 2px ring in `--ring` on every control, held to 3:1 against the field, glass and tints.
- `frontend/src/theme.test.ts` holds every text token to WCAG AA (4.5:1) in both schemes. It composites each glass and tinted pane over the page and over the center of each field blob, and checks the worst case, so the glass opacity can go down only as far as the text allows.

## Change the accent hue

Edit one line in `:root`:

```css
--accent-hue: 275; /* indigo */
```

Raise or lower `--accent-chroma` if the new hue looks dull or clips. Then run `pnpm test`. The theme test fails if text on or in the accent drops below AA, and until `public/favicon.svg` is redrawn in the new `--primary` (it prints the hex it expects).
