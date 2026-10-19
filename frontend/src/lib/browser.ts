export type BrowserKind = 'chrome' | 'other' | 'unknown'

type NavigatorLike = {
  userAgent?: string
  userAgentData?: { brands?: readonly { brand: string }[] }
}

/**
 * Chromium forks whose user agent carries `Chrome/` too. `Brave` and `Arc` never
 * appear in a user agent string (only in `userAgentData` brands), so without
 * brands they read as Chrome, which is the quiet failure we want.
 */
const CHROME_FORKS =
  /\b(Edg|EdgA|EdgiOS|OPR|OPT|Opera|Vivaldi|YaBrowser|SamsungBrowser|Brave|Arc)\b/

/**
 * Tells Google Chrome from other browsers, for the "netkeep needs Chrome" banner.
 *
 * `unknown` means "can't tell", and the banner stays quiet then: a false warning
 * costs more than a missed one. The structured brands list comes first because it
 * names Brave, which keeps `Chrome/` in its user agent and nothing else.
 */
export function detectBrowser(nav: NavigatorLike | undefined): BrowserKind {
  if (nav === undefined) return 'unknown'
  const brands = nav.userAgentData?.brands?.map((entry) => entry.brand) ?? []
  // Headless Chrome and Chrome for Testing are Chrome: a CDP screenshot of the UI
  // must not show the banner.
  if (brands.includes('Google Chrome') || brands.includes('Google Chrome for Testing')) {
    return 'chrome'
  }
  if (brands.some((brand) => brand === 'Chromium' || CHROME_FORKS.test(brand))) return 'other'

  const ua = nav.userAgent ?? ''
  if (/\b(Headless)?Chrome\//.test(ua)) return CHROME_FORKS.test(ua) ? 'other' : 'chrome'
  if (/\b(Firefox|FxiOS)\//.test(ua) || /\bSafari\//.test(ua)) return 'other'
  return 'unknown'
}
