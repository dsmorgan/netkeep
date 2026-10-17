import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'

import type { SupportLevel, SupportLevels } from './api'
import { supportBundleUrl } from './api'
import { SupportSection } from './support-section'

function level(name: SupportLevel['level'], extra: Partial<SupportLevel> = {}): SupportLevel {
  return {
    level: name,
    title: name.charAt(0).toUpperCase() + name.slice(1),
    summary: `${name} summary`,
    includes: [`${name} includes logs`],
    leaves_out: [`${name} leaves out secrets`],
    default_since_days: name === 'standard' ? 7 : 30,
    redacted: name !== 'full',
    needs_confirmation: name === 'full',
    warning: name === 'full' ? 'This file holds real names.' : null,
    ...extra,
  }
}

const LEVELS: SupportLevels = {
  levels: [level('standard'), level('detailed'), level('full')],
  max_since_days: 365,
}

function renderSection() {
  mockFetch((request) => {
    const { pathname } = new URL(request.url)
    if (pathname === '/api/v1/support/levels') return jsonResponse(LEVELS)
    return jsonResponse({ detail: 'unexpected' }, 500)
  })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <SupportSection />
    </QueryClientProvider>,
  )
}

describe('SupportSection', () => {
  it('previews the standard level and links its download', async () => {
    renderSection()
    expect(await screen.findByText('standard includes logs')).toBeInTheDocument()
    expect(screen.getByText('standard leaves out secrets')).toBeInTheDocument()
    const link = screen.getByTestId('support-download')
    expect(link).toHaveAttribute('href', '/api/v1/support/bundle?level=standard&since_days=7')
    expect(link).toHaveAttribute('download')
  })

  it('shows what another level holds, with its own default days', async () => {
    renderSection()
    fireEvent.click(await screen.findByLabelText(/^Detailed/))
    expect(screen.getByText('detailed includes logs')).toBeInTheDocument()
    expect(screen.getByTestId('support-download')).toHaveAttribute(
      'href',
      '/api/v1/support/bundle?level=detailed&since_days=30',
    )
  })

  it('needs the box ticked before the full level downloads', async () => {
    renderSection()
    fireEvent.click(await screen.findByLabelText(/^Full/))
    expect(screen.getByText('This file holds real names.')).toBeInTheDocument()
    expect(screen.queryByTestId('support-download')).toBeNull()
    expect(screen.getByRole('button', { name: 'Download the full bundle' })).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox'))
    expect(screen.getByTestId('support-download')).toHaveAttribute(
      'href',
      '/api/v1/support/bundle?level=full&since_days=30&confirm_full=true',
    )
  })

  it('takes the days you choose, within the limit', async () => {
    renderSection()
    const days = await screen.findByLabelText('Days of logs and runs')
    fireEvent.change(days, { target: { value: '3' } })
    expect(screen.getByTestId('support-download')).toHaveAttribute(
      'href',
      '/api/v1/support/bundle?level=standard&since_days=3',
    )
    fireEvent.change(days, { target: { value: '400' } })
    expect(screen.queryByTestId('support-download')).toBeNull()
    expect(screen.getByText('Between 1 and 365 days.')).toBeInTheDocument()
  })
})

describe('supportBundleUrl', () => {
  it('never sends the confirmation for a level that does not need it', () => {
    expect(supportBundleUrl('standard', 7, true)).toBe(
      '/api/v1/support/bundle?level=standard&since_days=7',
    )
  })
})
