import { describe, expect, it } from 'vitest'

import { detectBrowser } from './browser'

const CHROME_UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36'

describe('detectBrowser', () => {
  it('trusts the Google Chrome brand', () => {
    const userAgentData = { brands: [{ brand: 'Not.A/Brand' }, { brand: 'Google Chrome' }] }
    expect(detectBrowser({ userAgentData, userAgent: '' })).toBe('chrome')
  })

  it('flags Brave, which keeps Chrome/ in its user agent, by its brand', () => {
    const userAgentData = { brands: [{ brand: 'Chromium' }, { brand: 'Brave' }] }
    expect(detectBrowser({ userAgentData, userAgent: CHROME_UA })).toBe('other')
  })

  it('flags Edge by brand', () => {
    const userAgentData = { brands: [{ brand: 'Chromium' }, { brand: 'Microsoft Edge' }] }
    expect(detectBrowser({ userAgentData, userAgent: CHROME_UA })).toBe('other')
  })

  it('flags a fork named only by its brand, with no Chromium brand', () => {
    const userAgentData = { brands: [{ brand: 'Not.A/Brand' }, { brand: 'Microsoft Edge' }] }
    expect(detectBrowser({ userAgentData, userAgent: '' })).toBe('other')
  })

  it('counts headless Chrome and Chrome for Testing as Chrome', () => {
    expect(detectBrowser({ userAgent: CHROME_UA.replace('Chrome/', 'HeadlessChrome/') })).toBe(
      'chrome',
    )
    const userAgentData = {
      brands: [{ brand: 'Chromium' }, { brand: 'Google Chrome for Testing' }],
    }
    expect(detectBrowser({ userAgentData, userAgent: '' })).toBe('chrome')
  })

  it('reads Chrome from the user agent when there are no brands', () => {
    expect(detectBrowser({ userAgent: CHROME_UA })).toBe('chrome')
  })

  it.each(['Edg/154.0.0.0', 'OPR/110.0.0.0', 'Vivaldi/7.0'])(
    'flags a Chrome-like user agent that also says %s',
    (suffix) => {
      expect(detectBrowser({ userAgent: `${CHROME_UA} ${suffix}` })).toBe('other')
    },
  )

  it('flags Firefox and Safari', () => {
    expect(
      detectBrowser({
        userAgent:
          'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:130.0) Gecko/20100101 Firefox/130.0',
      }),
    ).toBe('other')
    expect(
      detectBrowser({
        userAgent:
          'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15',
      }),
    ).toBe('other')
  })

  it('does not guess when it cannot tell', () => {
    expect(detectBrowser(undefined)).toBe('unknown')
    expect(detectBrowser({ userAgent: '' })).toBe('unknown')
    expect(detectBrowser({ userAgent: 'Mozilla/5.0 (X11) jsdom/26.0.0' })).toBe('unknown')
  })
})
