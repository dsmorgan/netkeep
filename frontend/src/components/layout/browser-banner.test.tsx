import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { BROWSER_BANNER_KEY, BrowserBanner } from './browser-banner'

const FIREFOX_UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:130.0) Gecko/20100101 Firefox/130.0'
const CHROME_UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36'

beforeEach(() => window.localStorage.clear())
afterEach(() => {
  vi.restoreAllMocks()
  window.localStorage.clear()
})

describe('BrowserBanner', () => {
  it('warns in another browser, and Dismiss hides it and remembers', () => {
    vi.spyOn(window.navigator, 'userAgent', 'get').mockReturnValue(FIREFOX_UA)
    const { unmount } = render(<BrowserBanner />)
    expect(screen.getByRole('status')).toHaveTextContent(/built and tested for Google Chrome/)

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(window.localStorage.getItem(BROWSER_BANNER_KEY)).toBe('1')

    unmount()
    render(<BrowserBanner />)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('stays quiet in Chrome', () => {
    vi.spyOn(window.navigator, 'userAgent', 'get').mockReturnValue(CHROME_UA)
    render(<BrowserBanner />)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('still dismisses when storage throws', () => {
    vi.spyOn(window.navigator, 'userAgent', 'get').mockReturnValue(FIREFOX_UA)
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    render(<BrowserBanner />)
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })
})
