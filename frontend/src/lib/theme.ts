/**
 * The theme switch (#666): Auto, Light or Dark, remembered per browser.
 *
 * Auto, the default, leaves `data-theme` off <html> and the OS decides through
 * prefers-color-scheme; Light and Dark set it, and `index.css`'s `dark` variant
 * reads it. An inline script in `index.html` applies the saved choice before
 * the first paint, so a forced theme never flashes the OS one; it reads the
 * same key and has to stay in step with `applyTheme` (a test runs it).
 *
 * Every storage read and write is wrapped: `localStorage` throws in a private
 * window with site data blocked, and an unreadable choice is Auto.
 */
import { useCallback, useEffect, useState } from 'react'

export const THEME_CHOICES = ['auto', 'light', 'dark'] as const
export type ThemeChoice = (typeof THEME_CHOICES)[number]

export const THEME_KEY = 'netkeep.theme'

const isChoice = (value: unknown): value is ThemeChoice =>
  THEME_CHOICES.includes(value as ThemeChoice)

/** The saved choice, or Auto when there is none or it cannot be read. */
export function readTheme(): ThemeChoice {
  try {
    const saved = window.localStorage.getItem(THEME_KEY)
    return isChoice(saved) ? saved : 'auto'
  } catch {
    return 'auto'
  }
}

function saveTheme(choice: ThemeChoice): void {
  try {
    if (choice === 'auto') window.localStorage.removeItem(THEME_KEY)
    else window.localStorage.setItem(THEME_KEY, choice)
  } catch {
    // Storage is blocked: the choice holds for this page and is Auto on the next load.
  }
}

/** Set `data-theme`, and the color-scheme meta the browser paints the canvas from before CSS. */
export function applyTheme(choice: ThemeChoice, root: HTMLElement = document.documentElement) {
  if (choice === 'auto') delete root.dataset.theme
  else root.dataset.theme = choice
  const meta = root.ownerDocument.querySelector<HTMLMetaElement>('meta[name="color-scheme"]')
  if (meta) meta.content = choice === 'auto' ? 'light dark' : choice
}

/**
 * The current choice and a setter that applies and remembers it. A change saved
 * in another tab arrives as a `storage` event, and this tab follows it.
 */
export function useTheme(): [ThemeChoice, (choice: ThemeChoice) => void] {
  const [theme, setTheme] = useState(readTheme)
  useEffect(() => {
    const onStorage = (event: StorageEvent): void => {
      // A null key is `localStorage.clear()`, which also clears the choice.
      if (event.key !== THEME_KEY && event.key !== null) return
      const choice = readTheme()
      setTheme(choice)
      applyTheme(choice)
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])
  const choose = useCallback((choice: ThemeChoice) => {
    setTheme(choice)
    saveTheme(choice)
    applyTheme(choice)
  }, [])
  return [theme, choose]
}
