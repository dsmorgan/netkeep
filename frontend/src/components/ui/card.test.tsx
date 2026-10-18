import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { Card, CardHeader, CardTitle } from './card'

describe('CardTitle', () => {
  it.each([2, 3, 4] as const)('is a real level-%i heading', (level) => {
    render(
      <Card>
        <CardHeader>
          <CardTitle level={level}>Auto-tag rules</CardTitle>
        </CardHeader>
      </Card>,
    )
    const heading = screen.getByRole('heading', { name: 'Auto-tag rules', level })
    expect(heading.tagName).toBe(`H${level}`)
    // The slot the card's layout and the tests key on is kept.
    expect(heading).toHaveAttribute('data-slot', 'card-title')
  })
})

describe('Card', () => {
  it('is a plain glass pane by default', () => {
    render(<Card data-testid="pane" />)
    const pane = screen.getByTestId('pane')
    expect(pane).toHaveClass('nk-glass')
    expect(pane).not.toHaveClass('nk-tint')
    expect(pane).not.toHaveAttribute('data-tone')
  })

  it.each(['accent', 'ok', 'warn', 'bad'] as const)(
    'is tinted with its state color when it needs you (%s)',
    (tone) => {
      render(<Card data-testid="pane" tone={tone} />)
      const pane = screen.getByTestId('pane')
      expect(pane).toHaveClass('nk-glass', 'nk-tint', `nk-tone-${tone}`)
      expect(pane).toHaveAttribute('data-tone', tone)
    },
  )

  it('takes the lighter tray tint only when asked', () => {
    render(
      <>
        <Card data-testid="tray" tone="accent" tray />
        <Card data-testid="live" tone="accent" />
      </>,
    )
    expect(screen.getByTestId('tray')).toHaveClass('nk-tray')
    expect(screen.getByTestId('live')).not.toHaveClass('nk-tray')
  })
})
