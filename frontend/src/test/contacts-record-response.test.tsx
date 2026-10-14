/**
 * Recording a response that came outside netkeeper from the contact page (#512). The
 * contact, campaigns and note are invented.
 */
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { contactDetail, mockApi, type SeenRequest } from './contacts-fixtures'
import { jsonResponse } from './fetch'
import { renderApp } from './render'

const ENROLLMENTS = [
  {
    id: 31,
    campaign_id: 3,
    campaign_name: 'Autumn hello',
    status: 'active',
    current_step: 1,
    replied_at: null,
    exit_reason: null,
    can_still_send: true,
  },
  {
    id: 21,
    campaign_id: 2,
    campaign_name: 'Spring hello',
    status: 'paused',
    current_step: 2,
    replied_at: null,
    exit_reason: null,
    can_still_send: true,
  },
  {
    id: 11,
    campaign_id: 1,
    campaign_name: 'Winter hello',
    status: 'replied',
    current_step: 1,
    replied_at: '2026-01-05T12:00:00Z',
    exit_reason: 'replied',
    can_still_send: false,
  },
]

function serve(): SeenRequest[] {
  return mockApi((request) => {
    const { pathname } = new URL(request.url)
    if (pathname === '/api/v1/contacts/1' && request.method === 'GET') {
      return jsonResponse(contactDetail())
    }
    if (pathname === '/api/v1/contacts/1/tags') return jsonResponse([])
    if (pathname === '/api/v1/contacts/1/enrollments') return jsonResponse(ENROLLMENTS)
    if (pathname === '/api/v1/contacts/1/outside-response') {
      return jsonResponse(
        {
          interaction_id: 7,
          enrollments: [
            { id: 31, campaign_id: 3, status: 'replied', message_id: 70 },
            { id: 21, campaign_id: 2, status: 'replied', message_id: 71 },
          ],
        },
        201,
      )
    }
    return undefined
  })
}

async function openDialog() {
  await renderApp('/contacts/1')
  fireEvent.click(await screen.findByRole('button', { name: 'Record a response' }))
  return within(await screen.findByRole('dialog'))
}

describe('record a response from outside netkeeper', () => {
  it('lists the campaigns and ends every one that could still send by default', async () => {
    const seen = serve()
    await renderApp('/contacts/1')
    expect(await screen.findByRole('link', { name: 'Autumn hello' })).toBeVisible()
    expect(screen.getByRole('link', { name: 'Winter hello' })).toBeVisible()

    fireEvent.click(screen.getByRole('button', { name: 'Record a response' }))
    const dialog = within(await screen.findByRole('dialog'))
    expect(dialog.getByLabelText('What it ends')).toHaveDisplayValue(
      'All 2 of their campaigns that could still send',
    )
    // The replied one cannot be picked: it has ended.
    expect(dialog.queryByRole('option', { name: /Winter hello/ })).toBeNull()
    fireEvent.change(dialog.getByLabelText('How they responded'), { target: { value: 'text' } })
    fireEvent.change(dialog.getByLabelText('Note (optional)'), {
      target: { value: 'Texted back; keen to talk.' },
    })
    fireEvent.click(dialog.getByRole('button', { name: 'Record the response' }))

    expect(await dialog.findByText(/Recorded\. 2 enrollments/)).toBeVisible()
    const post = seen.find((r) => r.method === 'POST')
    expect(post?.body).toMatchObject({
      via: 'text',
      note: 'Texted back; keen to talk.',
      outcome: null,
      do_not_contact: false,
      enrollment_id: null,
    })
  })

  it('ends one campaign when you pick it, and an opt-out may mark them do not contact', async () => {
    const seen = serve()
    const dialog = await openDialog()
    fireEvent.change(dialog.getByLabelText('What it ends'), { target: { value: '21' } })
    fireEvent.change(dialog.getByLabelText('What they said (optional)'), {
      target: { value: 'opt_out' },
    })
    expect(dialog.getByText(/They end as opted out/)).toBeVisible()
    expect(dialog.getByRole('checkbox', { name: /do not contact/ })).toBeChecked()
    fireEvent.click(dialog.getByRole('button', { name: 'Record the response' }))

    await waitFor(() => expect(seen.some((r) => r.method === 'POST')).toBe(true))
    const post = seen.find((r) => r.method === 'POST')
    expect(post?.body).toMatchObject({
      via: 'call',
      outcome: 'opt_out',
      do_not_contact: true,
      enrollment_id: 21,
    })
  })

  it('refuses a time in the future before asking the server', async () => {
    const seen = serve()
    const dialog = await openDialog()
    fireEvent.change(dialog.getByLabelText('When'), { target: { value: '2999-01-01T10:00' } })
    expect(dialog.getByText('A response cannot be in the future.')).toBeVisible()
    expect(dialog.getByRole('button', { name: 'Record the response' })).toBeDisabled()
    expect(seen.some((r) => r.method === 'POST')).toBe(false)
  })
})
