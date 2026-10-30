import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'
import { renderApp } from '@/test/render'

import { ENROLLMENTS, campaign, campaignBackend, review } from './test-support'

const HELD = 'replies have not been polled recently'

describe('the enrollment row of a held follow-up (#716)', () => {
  it('says why it waits, and nothing on a row that is not held', async () => {
    mockFetch(
      campaignBackend(
        { campaign: campaign(), review: review({ status: 'draft' }) },
        {
          'GET /api/v1/campaigns/5/enrollments': () =>
            jsonResponse({
              ...ENROLLMENTS,
              items: [
                { ...ENROLLMENTS.items[0], current_step: 1, held_reason: HELD },
                { ...ENROLLMENTS.items[1], held_reason: null },
              ],
            }),
        },
      ),
    )
    await renderApp('/campaigns/5')

    const row = (await screen.findByRole('link', { name: 'Rosalind Quillfeather' })).closest('tr')
    expect(row as HTMLElement).toHaveTextContent(`Held until replies are read: ${HELD}`)
    const other = screen.getByRole('link', { name: 'Tobias Marrowbone' }).closest('tr')
    expect(other as HTMLElement).not.toHaveTextContent(/Held until replies are read/)
  })
})
