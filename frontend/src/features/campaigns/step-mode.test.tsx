/**
 * A running campaign's LinkedIn step switches between Prefill and Auto-send (#536): a
 * switch to Auto-send asks first and says what changes, a switch to Prefill does not,
 * and the page says when auto-send is off and nothing sends on its own.
 */
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'
import { renderApp } from '@/test/render'

import type { Campaign, StepModePreview } from './api'
import { STEPS, campaign, campaignBackend, review, type Call } from './test-support'

const LINKEDIN_STEP: Campaign['steps'][number] = {
  ...STEPS[0]!,
  id: 103,
  channel: 'linkedin',
  mode: 'prefill',
  template_id: 13,
  template_name: 'LinkedIn hello',
}

const CONFIRM_TEXT =
  "netkeeper sends this step's messages itself, within your active hours, spacing and budget."

function preview(overrides: Partial<StepModePreview> = {}): StepModePreview {
  return {
    campaign_id: 5,
    campaign_status: 'active',
    step_id: 103,
    position: 1,
    mode: 'prefill',
    auto_send_on: true,
    queued: 2,
    kept_prefill: 0,
    claimed: 0,
    prefilled: 1,
    refusal: null,
    confirm_text: CONFIRM_TEXT,
    ...overrides,
  }
}

const LINKEDIN_ROUTES = {
  'GET /api/v1/campaigns/linkedin/ready': () => jsonResponse({ items: [], total: 0, by_step: {} }),
  'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse({ items: [], total: 0 }),
  'GET /api/v1/linkedin/status': () =>
    jsonResponse({
      session_flag: null,
      session_flagged_at: null,
      heat_tripped: false,
      armed: true,
      schedule_paused: false,
      running_run_id: null,
      can_start_runs: true,
    }),
}

function setup(
  {
    status = 'active',
    mode = 'prefill',
    on = true,
    shown = preview(),
  }: {
    status?: Campaign['status']
    mode?: 'prefill' | 'auto_send'
    on?: boolean
    shown?: StepModePreview
  } = {},
  calls: Call[] = [],
) {
  const state = {
    campaign: campaign({ status, steps: [{ ...LINKEDIN_STEP, mode }], mailbox_id: null }),
    review: review({ status, missing: [] }),
  }
  const switched = (to: 'prefill' | 'auto_send', kept: number) => {
    state.campaign = campaign({
      status,
      steps: [{ ...LINKEDIN_STEP, mode: to, kept_prefill: kept }],
      mailbox_id: null,
    })
    return jsonResponse({
      from_mode: to === 'auto_send' ? 'prefill' : 'auto_send',
      to_mode: to,
      changed: true,
      kept_prefill: kept,
      auto_send_on: on,
      changed_at: '2030-06-18T13:00:00Z',
      campaign: state.campaign,
    })
  }
  mockFetch(
    campaignBackend(
      state,
      {
        ...LINKEDIN_ROUTES,
        'GET /api/v1/campaigns/linkedin/options': () => jsonResponse({ auto_send: on }),
        'GET /api/v1/campaigns/5/steps/103/mode': () => jsonResponse(shown),
        'PUT /api/v1/campaigns/5/steps/103/mode': (call) => {
          const body = call.body as { mode: 'prefill' | 'auto_send'; confirm?: boolean }
          if (body.mode === 'auto_send' && body.confirm !== true) {
            return jsonResponse({ detail: 'confirm the switch' }, 422)
          }
          return switched(body.mode, body.mode === 'auto_send' ? shown.queued : 0)
        },
      },
      calls,
    ),
  )
}

const puts = (calls: Call[]) => calls.filter((c) => c.method === 'PUT').map((c) => [c.path, c.body])

describe('switching a LinkedIn step mode', () => {
  it('asks before Auto-send, says what changes, and sends the confirm', async () => {
    const calls: Call[] = []
    setup({}, calls)
    await renderApp('/campaigns/5')

    fireEvent.click(await screen.findByRole('button', { name: 'Switch step 1 to Auto-send' }))
    const dialog = await screen.findByRole('alertdialog', {
      name: 'Switch step 1 to Auto-send?',
    })
    expect(await within(dialog).findByText(CONFIRM_TEXT)).toBeVisible()
    expect(
      await within(dialog).findByText(/2 contacts already waiting for this step stay on Prefill/),
    ).toBeVisible()
    expect(within(dialog).getByText(/1 message already typed into LinkedIn/)).toBeVisible()
    expect(within(dialog).getByText(/Switching sends nothing by itself/)).toBeVisible()
    expect(within(dialog).queryByText('Auto-send is off')).toBeNull()
    expect(puts(calls)).toEqual([]) // opening the confirm changes nothing

    fireEvent.click(within(dialog).getByRole('button', { name: 'Switch to Auto-send' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(puts(calls)).toEqual([
      ['/api/v1/campaigns/5/steps/103/mode', { mode: 'auto_send', confirm: true }],
    ])
    expect(await screen.findByText(/2 contacts already queued when it switched stay/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Switch step 1 to Prefill' })).toBeVisible()
  })

  it('sends nothing when the confirm is cancelled', async () => {
    const calls: Call[] = []
    setup({}, calls)
    await renderApp('/campaigns/5')

    fireEvent.click(await screen.findByRole('button', { name: 'Switch step 1 to Auto-send' }))
    const dialog = await screen.findByRole('alertdialog')
    await within(dialog).findByText(CONFIRM_TEXT)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(puts(calls)).toEqual([])
  })

  it('switches to Prefill without asking', async () => {
    const calls: Call[] = []
    setup({ mode: 'auto_send' }, calls)
    await renderApp('/campaigns/5')

    fireEvent.click(await screen.findByRole('button', { name: 'Switch step 1 to Prefill' }))
    await screen.findByRole('button', { name: 'Switch step 1 to Auto-send' })
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(puts(calls)).toEqual([
      ['/api/v1/campaigns/5/steps/103/mode', { mode: 'prefill', confirm: false }],
    ])
  })

  it('says so when auto-send is off: the switch is allowed and nothing sends', async () => {
    const calls: Call[] = []
    setup({ on: false, shown: preview({ auto_send_on: false }) }, calls)
    await renderApp('/campaigns/5')

    fireEvent.click(await screen.findByRole('button', { name: 'Switch step 1 to Auto-send' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(await within(dialog).findByText('Auto-send is off')).toBeVisible()
    expect(within(dialog).getByText(/sends nothing on its own until you turn it on/)).toBeVisible()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Switch to Auto-send' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    // The step row says it waits for a prefill while auto-send is off.
    expect(await screen.findByText('off in config.toml: waits for you to prefill')).toBeVisible()
  })

  it('cannot be confirmed when the switch is refused', async () => {
    const calls: Call[] = []
    setup({ shown: preview({ refusal: 'campaign 5 is completed' }) }, calls)
    await renderApp('/campaigns/5')

    fireEvent.click(await screen.findByRole('button', { name: 'Switch step 1 to Auto-send' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(await within(dialog).findByText(/campaign 5 is completed/)).toBeVisible()
    const confirm = within(dialog).getByRole('button', { name: 'Switch to Auto-send' })
    expect(confirm).toBeDisabled()
    fireEvent.click(confirm)
    expect(puts(calls)).toEqual([])
  })

  it.each(['draft', 'reviewing', 'completed', 'archived'] as const)(
    'offers no switch on a %s campaign',
    async (status) => {
      setup({ status })
      await renderApp('/campaigns/5')
      await screen.findByText('LinkedIn hello')
      expect(screen.queryByRole('button', { name: /Switch step 1/ })).toBeNull()
    },
  )

  it('offers no switch on an email step', async () => {
    mockFetch(
      campaignBackend({
        campaign: campaign({ status: 'active', missing: [] }),
        review: review({ status: 'active', missing: [] }),
      }),
    )
    await renderApp('/campaigns/5')
    await screen.findByText('Catching up')
    expect(screen.queryByRole('button', { name: /Switch step/ })).toBeNull()
  })
})
