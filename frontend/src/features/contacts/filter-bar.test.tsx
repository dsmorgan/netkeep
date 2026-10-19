import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { FilterBar } from './filter-bar'

function renderBar() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <FilterBar search={{}} onChange={vi.fn()} />
    </QueryClientProvider>,
  )
}

describe('FilterBar checkboxes (#659)', () => {
  it.each(['Do not contact', 'Include archived'])(
    'announces "%s" exactly as it reads on screen',
    (visible) => {
      renderBar()
      const box = screen.getByRole('checkbox', { name: visible })
      expect(box).not.toHaveAttribute('aria-label')
      expect(box.closest('label')).toHaveTextContent(visible)
    },
  )
})
