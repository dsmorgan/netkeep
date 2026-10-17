/**
 * "Make a list from this campaign" and the builder's guard preview (#513).
 */
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'
import { renderApp } from '@/test/render'

import { audiencePreview, campaign, campaignBackend, review, type Call } from './test-support'

const DONE = campaign({ status: 'completed', enrollments: { replied: 2, completed: 3 } })

function backend(calls: Call[] = [], overrides = {}) {
  return campaignBackend(
    { campaign: DONE, review: review({ status: 'completed', missing: [] }) },
    {
      'POST /api/v1/campaigns/5/lists': (call: Call) =>
        jsonResponse(
          {
            list_id: 31,
            name: 'Autumn hello: no reply',
            kind: (call.body as { kind: string }).kind,
            members: 3,
          },
          201,
        ),
      ...overrides,
    },
    calls,
  )
}

async function listsCard(): Promise<HTMLElement> {
  const heading = await screen.findByRole('heading', { name: 'Make a list from this campaign' })
  return heading.closest('[data-slot="card"]') as HTMLElement
}

describe('make a list from a campaign', () => {
  it('shows one button per outcome with its count', async () => {
    mockFetch(backend())
    await renderApp('/campaigns/5')

    const card = within(await listsCard())
    expect(await card.findByRole('button', { name: 'Replied (2) → new list' })).toBeEnabled()
    expect(card.getByRole('button', { name: 'No reply (3) → new list' })).toBeEnabled()
    expect(
      card.getByRole('button', { name: 'Completed without reply (2) → new list' }),
    ).toBeVisible()
    // An empty snapshot would be a list of nobody.
    expect(card.getByRole('button', { name: 'Opted out (0) → new list' })).toBeDisabled()
    expect(card.getAllByRole('button', { name: /→ new list$/ })).toHaveLength(7)
  })

  it('makes a static snapshot by default and links to a campaign from it', async () => {
    const calls: Call[] = []
    mockFetch(backend(calls))
    const { router } = await renderApp('/campaigns/5')

    const card = within(await listsCard())
    expect(card.getByRole('radio', { name: /A snapshot/ })).toBeChecked()
    fireEvent.click(await card.findByRole('button', { name: 'No reply (3) → new list' }))

    expect(await card.findByText('Made “Autumn hello: no reply”')).toBeVisible()
    const post = calls.find((c) => c.method === 'POST' && c.path === '/api/v1/campaigns/5/lists')
    expect(post?.body).toEqual({ outcome: 'no_reply', kind: 'static' })

    fireEvent.click(card.getByRole('link', { name: 'Start a campaign from it' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/campaigns/new'))
    expect(router.state.location.search).toEqual({ list: 31 })
    expect(await screen.findByLabelText('List')).toHaveValue('')
  })

  it('offers a live list instead', async () => {
    const calls: Call[] = []
    mockFetch(backend(calls))
    await renderApp('/campaigns/5')

    const card = within(await listsCard())
    fireEvent.click(card.getByRole('radio', { name: /Live/ }))
    fireEvent.click(await card.findByRole('button', { name: 'Replied (2) → new list' }))
    await card.findByText(/updates as the campaign moves on/)
    const post = calls.find((c) => c.method === 'POST' && c.path === '/api/v1/campaigns/5/lists')
    expect(post?.body).toEqual({ outcome: 'replied', kind: 'smart' })
  })

  it('says why a list was not made', async () => {
    mockFetch(
      backend([], {
        'POST /api/v1/campaigns/5/lists': () =>
          jsonResponse({ detail: "a list named 'Follow up' already exists" }, 409),
      }),
    )
    await renderApp('/campaigns/5')
    const card = within(await listsCard())
    fireEvent.click(await card.findByRole('button', { name: 'Bounced (1) → new list' }))
    expect(await card.findByText(/already exists/)).toBeVisible()
  })

  it('does not show on a draft', async () => {
    mockFetch(campaignBackend({ campaign: campaign(), review: review() }))
    await renderApp('/campaigns/5')
    await screen.findByRole('heading', { name: 'Audience' })
    expect(screen.queryByRole('heading', { name: 'Make a list from this campaign' })).toBeNull()
  })
})

describe('the builder previews the guards', () => {
  it('counts what each guard would skip for the chosen list and first channel', async () => {
    const calls: Call[] = []
    mockFetch(backend(calls))
    await renderApp('/campaigns/new?list=21')

    // The link from a campaign's page arrives with the list chosen.
    expect(await screen.findByLabelText('List')).toHaveValue('21')
    expect(await screen.findByText(/Pick the first step's template to see how many/)).toBeVisible()

    await screen.findAllByRole('option', { name: 'Catching up (email)' })
    const first = within(screen.getByRole('listitem', { name: 'Step 1' }))
    fireEvent.change(first.getByLabelText('Template'), { target: { value: '11' } })

    const preview = within(await screen.findByRole('region', { name: 'Guard preview' }))
    expect(preview.getByText('Of 5 contacts, 2 would start.')).toBeVisible()
    expect(preview.getByText('2 skipped: in another campaign')).toBeVisible()
    expect(preview.getByText('1 skipped: do-not-contact')).toBeVisible()
    const post = calls.find(
      (c) => c.method === 'POST' && c.path === '/api/v1/campaigns/audience-preview',
    )
    expect(post?.body).toEqual({ list_id: 21, channel: 'email' })
  })

  it('never asks without a list or a complete filter, and never flashes an error', async () => {
    const calls: Call[] = []
    mockFetch(backend(calls))
    await renderApp('/campaigns/new')
    await screen.findAllByRole('option', { name: 'Catching up (email)' })
    const first = within(screen.getByRole('listitem', { name: 'Step 1' }))
    fireEvent.change(first.getByLabelText('Template'), { target: { value: '11' } })

    // A list chosen right after "A list": the debounced copy once held no list at all.
    fireEvent.change(screen.getByLabelText('Audience from'), { target: { value: 'list' } })
    await screen.findByRole('option', { name: 'Old colleagues (3)' })
    fireEvent.change(screen.getByLabelText('List'), { target: { value: '21' } })
    await screen.findByRole('region', { name: 'Guard preview' })

    // A filter with no condition yet is not a source to ask about.
    fireEvent.change(screen.getByLabelText('Audience from'), { target: { value: 'filter' } })
    await new Promise((resolve) => setTimeout(resolve, 500))

    const asked = calls.filter(
      (c) => c.method === 'POST' && c.path === '/api/v1/campaigns/audience-preview',
    )
    expect(asked.length).toBeGreaterThan(0)
    for (const call of asked) {
      const body = call.body as { list_id?: number | null; filter?: { where: unknown } | null }
      const hasList = typeof body.list_id === 'number'
      const hasFilter =
        body.filter !== undefined && body.filter !== null && body.filter.where !== null
      expect(hasList || hasFilter, JSON.stringify(body)).toBe(true)
    }
    expect(screen.queryByText('The guard preview is unavailable.')).toBeNull()
  })

  it('says so when no guard would skip anyone', async () => {
    mockFetch(
      backend([], {
        'POST /api/v1/campaigns/audience-preview': () =>
          jsonResponse(audiencePreview({ will_start: 5, skipped: [] })),
      }),
    )
    await renderApp('/campaigns/new?list=21')
    await screen.findAllByRole('option', { name: 'Catching up (email)' })
    const first = within(screen.getByRole('listitem', { name: 'Step 1' }))
    fireEvent.change(first.getByLabelText('Template'), { target: { value: '11' } })
    expect(await screen.findByText('No guard would skip anyone.')).toBeVisible()
  })
})
