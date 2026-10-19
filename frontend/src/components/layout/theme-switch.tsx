import { useId } from 'react'

import { THEME_CHOICES, useTheme, type ThemeChoice } from '@/lib/theme'

const LABELS: Record<ThemeChoice, string> = { auto: 'Auto', light: 'Light', dark: 'Dark' }

/**
 * Auto, Light or Dark (#666), in the sidebar's footer. A native radio group, so
 * Tab reaches the chosen one and the arrow keys move between them.
 */
export function ThemeSwitch() {
  const [theme, setTheme] = useTheme()
  const name = useId()
  return (
    <fieldset className="flex flex-col gap-1.5">
      <legend className="mb-1.5 text-xs font-semibold text-sidebar-foreground">Theme</legend>
      <div className="grid grid-cols-3 gap-px rounded-md bg-sidebar-border p-0.5">
        {THEME_CHOICES.map((choice) => (
          <label
            key={choice}
            className="cursor-pointer rounded-[5px] bg-sidebar px-1 py-1 text-center text-xs text-sidebar-foreground hover:text-sidebar-accent-foreground has-checked:bg-sidebar-accent has-checked:font-semibold has-checked:text-sidebar-accent-foreground has-focus-visible:outline-2 has-focus-visible:outline-offset-1 has-focus-visible:outline-sidebar-ring"
          >
            <input
              type="radio"
              name={name}
              value={choice}
              checked={theme === choice}
              onChange={() => setTheme(choice)}
              className="sr-only"
            />
            {LABELS[choice]}
          </label>
        ))}
      </div>
    </fieldset>
  )
}
