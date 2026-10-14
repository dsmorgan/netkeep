/**
 * **Record a response** on a campaign's enrollment row (#512). The contacts are
 * `test-support`'s invented ones.
 */
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { Call } from '@/features/imports/test-support'
import { jsonResponse, mockFetch } from '@/test/fetch'
import { renderApp } from '@/test/render'

import { campaign, campaignBackend, review } from './test-support'

describe('record a response on an enrollment row', () => {
  it('is offered on a row that could still send, and ends every campaign by default', async () => {
    const calls: Call[] = []
    mockFetch(
      campaignBackend(
        {
          campaign: campaign({ status: 'active', enrollments: { active: 1, replied: 1 } }),
          review: review({ status: 'active', missing: [] }),
        },
        {
          'POST /api/v1/contacts/401/outside-response': () =>
            jsonResponse(
              {
                interaction_id: 9,
                enrollments: [{ id: 301, campaign_id: 5, status: 'replied', message_id: 90 }],
              },
              201,
            ),
        },
        calls,
      ),
    )
    await renderApp('/campaigns/5')

    const rosalind = (await screen.findByRole('link', { name: 'Rosalind Quillfeather' })).closest(
      'tr',
    ) as HTMLElement
    const tobias = screen
      .getByRole('link', { name: 'Tobias Marrowbone' })
      .closest('tr') as HTMLElement
    // Tobias already replied: there is nothing left to end.
    expect(within(tobias).queryByRole('button', { name: 'Record a response' })).toBeNull()

    fireEvent.click(within(rosalind).getByRole('button', { name: 'Record a response' }))
    const dialog = within(await screen.findByRole('dialog'))
    const scope = dialog.getByLabelText('What it ends')
    expect(scope).toHaveDisplayValue('Every campaign of theirs that could still send')
    expect(dialog.getByRole('option', { name: /^Only Autumn reconnect/ })).toBeInTheDocument()
    fireEvent.change(dialog.getByLabelText('How they responded'), {
      target: { value: 'in_person' },
    })
    fireEvent.click(dialog.getByRole('button', { name: 'Record the response' }))

    expect(await dialog.findByText(/Recorded\. 1 enrollment/)).toBeVisible()
    await waitFor(() =>
      expect(calls.some((c) => c.path === '/api/v1/contacts/401/outside-response')).toBe(true),
    )
    const post = calls.find((c) => c.path === '/api/v1/contacts/401/outside-response')
    expect(post?.body).toMatchObject({ via: 'in_person', enrollment_id: null })
  })
})
