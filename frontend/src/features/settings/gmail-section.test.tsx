import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { automationKeys } from '@/features/automation/api'
import { navigation } from '@/features/mailboxes/api'
import { armSteps, mailbox, renderWithBackend, status } from '@/features/mailboxes/test-support'
import { pollStatusKeys } from '@/features/poll-status/api'
import { resetFakeEventSource } from '@/test/fake-event-source'
import { jsonResponse } from '@/test/fetch'

import { GmailSection, type GmailOutcome } from './gmail-section'

afterEach(() => {
  resetFakeEventSource()
  vi.restoreAllMocks()
})

function renderSection(
  current: () => ReturnType<typeof status>,
  routes: Parameters<typeof renderWithBackend>[2] = {},
  outcome: GmailOutcome = {},
) {
  return renderWithBackend(<GmailSection outcome={outcome} />, current, routes)
}

describe('GmailSection', () => {
  it('asks for the OAuth client in the guide, and cannot connect without one', async () => {
    renderSection(() => status({ client_configured: false, client_id: null }))
    fireEvent.click(await screen.findByRole('button', { name: /Save the client in netkeep/ }))
    expect(screen.getByLabelText('Client ID')).toBeInTheDocument()
    expect(screen.getByLabelText('Client secret')).toHaveAttribute('type', 'password')
    expect(screen.queryByRole('region', { name: 'OAuth client' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Connect your mailbox/ }))
    expect(screen.getByRole('button', { name: 'Connect Gmail' })).toBeDisabled()
    expect(screen.getByText('Save the OAuth client first.')).toBeInTheDocument()
  })

  it('saves the client, then offers to connect', async () => {
    let current = status({ client_configured: false, client_id: null })
    const { calls } = renderSection(() => current, {
      'PUT /api/v1/mailboxes/oauth/client': () => {
        current = status()
        return jsonResponse(current)
      },
    })
    fireEvent.click(await screen.findByRole('button', { name: /Save the client in netkeep/ }))
    fireEvent.change(screen.getByLabelText('Client ID'), {
      target: { value: 'abc.apps.googleusercontent.com' },
    })
    fireEvent.change(screen.getByLabelText('Client secret'), { target: { value: 's3cret' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save client' }))

    await waitFor(() =>
      expect(calls).toContainEqual({
        method: 'PUT',
        path: '/api/v1/mailboxes/oauth/client',
        body: { client_id: 'abc.apps.googleusercontent.com', client_secret: 's3cret' },
      }),
    )
    expect(await screen.findByText('1234-fake.apps.googleusercontent.com')).toBeInTheDocument()
    expect(screen.queryByLabelText('Client ID')).not.toBeInTheDocument()
    expect(screen.queryByText('s3cret')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Connect your mailbox/ }))
    expect(screen.getByRole('button', { name: 'Connect Gmail' })).toBeEnabled()
  })

  it('shows why a client was refused', async () => {
    renderSection(() => status({ client_configured: false, client_id: null }), {
      'PUT /api/v1/mailboxes/oauth/client': () =>
        jsonResponse(
          { detail: 'a Google OAuth client ID ends in .apps.googleusercontent.com' },
          422,
        ),
    })
    fireEvent.click(await screen.findByRole('button', { name: /Save the client in netkeep/ }))
    fireEvent.change(screen.getByLabelText('Client ID'), { target: { value: 'my-project' } })
    fireEvent.change(screen.getByLabelText('Client secret'), { target: { value: 's' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save client' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'ends in .apps.googleusercontent.com',
    )
  })

  it('connects by going to Google’s page', async () => {
    const assign = vi.spyOn(navigation, 'assign').mockImplementation(() => {})
    const { calls } = renderSection(() => status(), {
      'POST /api/v1/mailboxes/oauth/start': () =>
        jsonResponse({ authorization_url: 'https://accounts.example/auth' }),
    })
    fireEvent.click(await screen.findByRole('button', { name: /Connect your mailbox/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Connect Gmail' }))
    await waitFor(() => expect(assign).toHaveBeenCalledWith('https://accounts.example/auth'))
    expect(calls).toContainEqual({
      method: 'POST',
      path: '/api/v1/mailboxes/oauth/start',
      body: { mailbox_id: null },
    })
  })

  it('shows a connected mailbox and checks it on demand', async () => {
    const { calls } = renderSection(() => status({ mailboxes: [mailbox()] }), {
      'POST /api/v1/mailboxes/3/check': () => jsonResponse(mailbox()),
    })
    const item = (await screen.findByText('sender@example.com')).closest('li') as HTMLElement
    expect(within(item).getByText('connected')).toBeInTheDocument()
    expect(item).toHaveTextContent('Up to 80 recipients a day')
    expect(screen.queryByRole('button', { name: 'Connect Gmail' })).not.toBeInTheDocument()

    fireEvent.click(within(item).getByRole('button', { name: 'Check now' }))
    await waitFor(() =>
      expect(calls).toContainEqual({
        method: 'POST',
        path: '/api/v1/mailboxes/3/check',
        body: null,
      }),
    )
  })

  it('re-authorizes a mailbox that needs it, preselecting its account', async () => {
    vi.spyOn(navigation, 'assign').mockImplementation(() => {})
    const { calls } = renderSection(
      () =>
        status({
          mailboxes: [mailbox({ status: 'reauth_required', status_reason: 'invalid_grant' })],
        }),
      {
        'POST /api/v1/mailboxes/oauth/start': () =>
          jsonResponse({ authorization_url: 'https://accounts.example/auth' }),
      },
    )
    const item = (await screen.findByText('sender@example.com')).closest('li') as HTMLElement
    expect(within(item).getByText('needs re-authorizing')).toBeInTheDocument()
    expect(item).toHaveTextContent(/access was revoked/)
    fireEvent.click(within(item).getByRole('button', { name: 'Re-authorize' }))
    await waitFor(() =>
      expect(calls).toContainEqual({
        method: 'POST',
        path: '/api/v1/mailboxes/oauth/start',
        body: { mailbox_id: 3 },
      }),
    )
  })

  it('disconnects only after confirming', async () => {
    let current = status({ mailboxes: [mailbox()] })
    const { calls, queryClient } = renderSection(() => current, {
      'POST /api/v1/mailboxes/3/disconnect': () => {
        current = status({
          mailboxes: [mailbox({ status: 'disabled', status_reason: 'disconnected' })],
        })
        return jsonResponse(current.mailboxes[0])
      },
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Disconnect' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('forgets sender@example.com’s token')
    expect(calls.some((call) => call.path.endsWith('/disconnect'))).toBe(false)

    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Disconnect' }))
    expect(await screen.findByText('disconnected')).toBeInTheDocument()
    // Disconnecting moves the mailbox Off, so the switch reads again (#829).
    expect(invalidate).toHaveBeenCalledWith({ queryKey: automationKeys.all })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: pollStatusKeys.all })
    expect(screen.getByText(/Connect one in step 7 of the setup guide/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Connect your mailbox/ }))
    expect(screen.getByRole('button', { name: 'Connect Gmail' })).toBeEnabled()
  })

  it.each([
    [null, 'Off', 'Off: netkeep serve makes no drafts and sends nothing from it.'],
    ['draft', 'Manual', /^Manual since .* by web \(user 1\): every step becomes a draft/],
    ['send', 'Automatic', /^Automatic since .* by web \(user 1\): send steps go out/],
  ] as const)(
    'has no arm buttons on a mailbox at %s: the switch is the one way there (#834)',
    async (arm, label, text) => {
      const { calls, queryClient } = renderSection(
        () =>
          status({
            mailboxes: [
              mailbox({
                arm,
                armed_at: arm === null ? null : '2026-09-27T12:00:00Z',
                armed_by: arm === null ? null : 'web (user 1)',
              }),
            ],
          }),
        { 'POST /api/v1/mailboxes/3/check': () => jsonResponse(mailbox()) },
      )
      const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
      const item = (await screen.findByText('sender@example.com')).closest('li') as HTMLElement
      // The mailbox says where it stands in the switch's own words.
      expect(within(item).getByText(label)).toBeInTheDocument()
      expect(within(item).getByText(text)).toBeInTheDocument()
      expect(item).not.toHaveTextContent(/armed/i)
      // Check now and Disconnect stay; nothing arms, disarms or confirms an arming.
      const buttons = within(item)
        .getAllByRole('button')
        .map((button) => button.textContent)
      expect(buttons).toEqual(['Check now', 'Disconnect'])
      expect(screen.queryByRole('button', { name: /arm/i })).toBeNull()

      fireEvent.click(within(item).getByRole('button', { name: 'Check now' }))
      await waitFor(() =>
        expect(calls).toContainEqual({
          method: 'POST',
          path: '/api/v1/mailboxes/3/check',
          body: null,
        }),
      )
      // The header's poll status changes too (#401), and so may whether Gmail Automatic
      // is ready (#829).
      await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: pollStatusKeys.all }))
      expect(invalidate).toHaveBeenCalledWith({ queryKey: automationKeys.all })
      expect(calls.some((call) => /\/(arm|disarm)$/.test(call.path))).toBe(false)
    },
  )

  it('waits for a draft found by its Message-ID before Automatic is available', async () => {
    renderSection(() =>
      status({
        mailboxes: [mailbox({ arm: 'draft', armed_at: '2026-09-27T12:00:00Z' })],
        arm_steps: [armSteps({ test_campaign_id: 7 })],
      }),
    )
    const path = await screen.findByRole('region', { name: 'Steps to Automatic' })
    expect(
      within(path).getByText(
        'Automatic is unavailable until netkeep finds one of its own drafts in this mailbox.',
      ),
    ).toBeInTheDocument()
    expect(within(path).getByText(/Waiting: no draft yet/)).toBeInTheDocument()
    expect(within(path).getByRole('link', { name: 'Send a test' })).toHaveAttribute(
      'href',
      '/campaigns/7',
    )
    expect(path).toHaveTextContent('Gmail on Manual. Done.')
    expect(path).toHaveTextContent('Gmail on Automatic. Not yet: waits for the steps above.')
    expect(path).toHaveTextContent('Off takes all of that back')
    expect(path).not.toHaveTextContent(/\b(Arm|Disarm)\b/)
  })

  it('points at the switch while Gmail is Off', async () => {
    renderSection(() =>
      status({
        mailboxes: [mailbox()],
        arm_steps: [
          armSteps({ armed_for_drafts: false, waiting: 'not_armed', next_step: 'arm_drafts' }),
        ],
      }),
    )
    const path = await screen.findByRole('region', { name: 'Steps to Automatic' })
    expect(path).toHaveTextContent(
      'Gmail on Manual. Not yet. Move Gmail to Manual under Automation.',
    )
    expect(path).toHaveTextContent(
      'Waits until Gmail is on Manual: netkeep checks only on Manual or Automatic.',
    )
  })

  it('shows each reason the check waits, and the next step for it', async () => {
    let current = status({
      mailboxes: [mailbox({ arm: 'draft', armed_at: '2026-09-27T12:00:00Z' })],
      arm_steps: [
        armSteps({
          waiting: 'not_found',
          checked_at: '2026-09-28T12:00:00Z',
          next_step: 'check_now',
        }),
      ],
    })
    const { calls } = renderSection(() => current, {
      'POST /api/v1/mailboxes/3/check': () => {
        current = status({
          mailboxes: current.mailboxes,
          arm_steps: [
            armSteps({
              waiting: 'not_found',
              checked_at: '2026-09-28T12:00:00Z',
              check_requested_at: '2026-09-28T12:05:00Z',
              next_step: 'check_now',
            }),
          ],
        })
        return jsonResponse(current.mailboxes[0])
      },
    })
    const path = await screen.findByRole('region', { name: 'Steps to Automatic' })
    const last = new Date('2026-09-28T12:00:00Z').toLocaleString()
    expect(
      within(path).getByText(`Waiting: not found yet. Last checked ${last}.`),
    ).toBeInTheDocument()
    // One Check now, in the steps to Automatic, not a second one beside Disconnect.
    expect(screen.getAllByRole('button', { name: 'Check now' })).toHaveLength(1)
    fireEvent.click(within(path).getByRole('button', { name: 'Check now' }))
    expect(await within(path).findByText(/A check runs within a minute/)).toBeInTheDocument()
    expect(calls).toContainEqual({ method: 'POST', path: '/api/v1/mailboxes/3/check', body: null })
  })

  it('says when the check waits for you to sign in again', async () => {
    renderSection(() =>
      status({
        mailboxes: [
          mailbox({
            arm: 'draft',
            armed_at: '2026-09-27T12:00:00Z',
            status: 'reauth_required',
            status_reason: 'invalid_grant',
          }),
        ],
        arm_steps: [armSteps({ waiting: 'reauth_required', next_step: 'reauthorize' })],
      }),
    )
    const path = await screen.findByRole('region', { name: 'Steps to Automatic' })
    expect(within(path).getByText(/the mailbox needs you to sign in again/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Re-authorize' })).toBeEnabled()
  })

  it('says when nothing checks because serve is not running', async () => {
    renderSection(() =>
      status({
        mailboxes: [mailbox({ arm: 'draft', armed_at: '2026-09-27T12:00:00Z' })],
        arm_steps: [
          armSteps({ waiting: 'not_checked', next_step: 'check_now', checks_running: false }),
        ],
      }),
    )
    const path = await screen.findByRole('region', { name: 'Steps to Automatic' })
    expect(within(path).getByText(/netkeep serve isn’t running/)).toBeInTheDocument()
  })

  it('says Automatic is ready once the draft is found, and points at the switch', async () => {
    renderSection(() =>
      status({
        mailboxes: [
          mailbox({
            arm: 'draft',
            armed_at: '2026-09-27T12:00:00Z',
            message_id_verified_at: '2026-09-27T12:10:00Z',
          }),
        ],
        arm_steps: [
          armSteps({
            verified_at: '2026-09-27T12:10:00Z',
            waiting: null,
            can_arm_send: true,
            next_step: 'arm_send',
          }),
        ],
      }),
    )
    const found = new Date('2026-09-27T12:10:00Z').toLocaleString()
    expect(await screen.findByText(`Found ${found}.`)).toBeInTheDocument()
    const path = screen.getByRole('region', { name: 'Steps to Automatic' })
    expect(path).toHaveTextContent(
      'Gmail on Automatic. Ready. Move Gmail to Automatic under Automation.',
    )
    expect(within(path).queryByText(/unavailable/)).toBeNull()
    expect(screen.queryByRole('button', { name: /arm/i })).toBeNull()
  })

  it('reports the callback’s outcome', async () => {
    const { unmount } = renderSection(() => status(), {}, { gmail: 'connected' })
    expect(await screen.findByText('Gmail is connected.')).toBeInTheDocument()
    unmount()

    renderSection(() => status(), {}, { gmail: 'error', reason: 'gmail_api_refused' })
    expect(await screen.findByText(/Enable the Gmail API in the Cloud project/)).toBeInTheDocument()
  })

  it('names an unknown reason as Google’s own', async () => {
    renderSection(() => status(), {}, { gmail: 'error', reason: 'temporarily_unavailable' })
    expect(await screen.findByText(/Google answered "temporarily_unavailable"/)).toBeInTheDocument()
  })
})
