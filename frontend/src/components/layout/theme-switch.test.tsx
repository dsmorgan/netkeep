import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { THEME_KEY } from '@/lib/theme'

import indexHtml from '../../../index.html?raw'

import { ThemeSwitch } from './theme-switch'

const root = document.documentElement

function colorSchemeMeta(): HTMLMetaElement {
  return document.querySelector('meta[name="color-scheme"]') as HTMLMetaElement
}

beforeEach(() => {
  const meta = document.createElement('meta')
  meta.name = 'color-scheme'
  meta.content = 'light dark'
  document.head.append(meta)
})

afterEach(() => {
  window.localStorage.clear()
  delete root.dataset.theme
  colorSchemeMeta().remove()
  vi.restoreAllMocks()
})

const radio = (name: string) =>
  within(screen.getByRole('group', { name: 'Theme' })).getByRole('radio', { name })

describe('the theme switch', () => {
  it('is a labeled radio group of Auto, Light and Dark, Auto by default', () => {
    render(<ThemeSwitch />)

    const radios = within(screen.getByRole('group', { name: 'Theme' })).getAllByRole('radio')
    expect(radios.map((r) => r.closest('label')?.textContent)).toEqual(['Auto', 'Light', 'Dark'])
    // One shared name makes them one native group: Tab stops on the checked one and the arrows move.
    expect(new Set(radios.map((r) => r.getAttribute('name'))).size).toBe(1)
    expect(radio('Auto')).toBeChecked()
    expect(root).not.toHaveAttribute('data-theme')
    expect(colorSchemeMeta().content).toBe('light dark')
  })

  it('applies a choice to <html> and remembers it', () => {
    render(<ThemeSwitch />)

    fireEvent.click(radio('Dark'))
    expect(radio('Dark')).toBeChecked()
    expect(root).toHaveAttribute('data-theme', 'dark')
    expect(colorSchemeMeta().content).toBe('dark')
    expect(window.localStorage.getItem(THEME_KEY)).toBe('dark')

    fireEvent.click(radio('Light'))
    expect(root).toHaveAttribute('data-theme', 'light')
    expect(colorSchemeMeta().content).toBe('light')
    expect(window.localStorage.getItem(THEME_KEY)).toBe('light')
  })

  it('goes back to following the OS on Auto', () => {
    window.localStorage.setItem(THEME_KEY, 'dark')
    root.dataset.theme = 'dark'
    render(<ThemeSwitch />)

    fireEvent.click(radio('Auto'))
    expect(root).not.toHaveAttribute('data-theme')
    expect(colorSchemeMeta().content).toBe('light dark')
    expect(window.localStorage.getItem(THEME_KEY)).toBeNull()
  })

  it('shows the saved choice', () => {
    window.localStorage.setItem(THEME_KEY, 'light')
    render(<ThemeSwitch />)
    expect(radio('Light')).toBeChecked()
  })

  it('reads an unknown saved value as Auto', () => {
    window.localStorage.setItem(THEME_KEY, 'sepia')
    render(<ThemeSwitch />)
    expect(radio('Auto')).toBeChecked()
  })

  it('follows a change saved in another tab', () => {
    render(<ThemeSwitch />)

    window.localStorage.setItem(THEME_KEY, 'dark')
    act(() => {
      window.dispatchEvent(new StorageEvent('storage', { key: THEME_KEY, newValue: 'dark' }))
    })
    expect(radio('Dark')).toBeChecked()
    expect(root).toHaveAttribute('data-theme', 'dark')

    window.localStorage.removeItem(THEME_KEY)
    act(() => {
      window.dispatchEvent(new StorageEvent('storage', { key: THEME_KEY, newValue: null }))
    })
    expect(radio('Auto')).toBeChecked()
    expect(root).not.toHaveAttribute('data-theme')
  })

  it('ignores other keys changing in another tab', () => {
    render(<ThemeSwitch />)
    window.localStorage.setItem(THEME_KEY, 'dark')
    act(() => {
      window.dispatchEvent(new StorageEvent('storage', { key: 'netkeeper.other' }))
    })
    expect(radio('Auto')).toBeChecked()
  })

  it('still switches when storage is blocked', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    render(<ThemeSwitch />)
    expect(radio('Auto')).toBeChecked()

    fireEvent.click(radio('Dark'))
    expect(root).toHaveAttribute('data-theme', 'dark')
  })
})

describe("index.html's before-paint script", () => {
  const script = /<script>([\s\S]*?)<\/script>/.exec(indexHtml)?.[1] ?? ''
  const run = () => {
    new Function(script)()
  }

  it('reads the key the switch writes', () => {
    expect(script).toContain(`'${THEME_KEY}'`)
  })

  it.each(['light', 'dark'])('applies a saved %s before React loads', (choice) => {
    window.localStorage.setItem(THEME_KEY, choice)
    run()
    expect(root).toHaveAttribute('data-theme', choice)
    expect(colorSchemeMeta().content).toBe(choice)
  })

  it.each([null, 'sepia'])('leaves Auto alone when the saved value is %s', (saved) => {
    if (saved !== null) window.localStorage.setItem(THEME_KEY, saved)
    run()
    expect(root).not.toHaveAttribute('data-theme')
    expect(colorSchemeMeta().content).toBe('light dark')
  })

  it('leaves no global behind', () => {
    window.localStorage.setItem(THEME_KEY, 'dark')
    // An indirect eval runs at the top level, as the page's classic script does.
    ;(0, eval)(script)
    expect(root).toHaveAttribute('data-theme', 'dark')
    expect('theme' in globalThis).toBe(false)
  })

  it('survives blocked storage', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    expect(run).not.toThrow()
    expect(root).not.toHaveAttribute('data-theme')
  })
})
