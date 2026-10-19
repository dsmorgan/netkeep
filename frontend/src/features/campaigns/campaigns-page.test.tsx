import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'
import { renderApp } from '@/test/render'

import { formatWhen } from '@/features/campaigns/format'
import { SCHEDULE_ARMED, autoSendOutlook } from '@/features/linkedin/test-support'
import { mailbox } from '@/features/mailboxes/test-support'

import {
  ENROLLMENTS,
  MAILBOX,
  STEPS,
  TEMPLATES,
  campaign,
  campaignBackend,
  review,
  summary,
  type Call,
} from './test-support'

function rowOf(name: string): HTMLElement {
  const row = screen.getByRole('link', { name }).closest('tr')
  expect(row).not.toBeNull()
  return row as HTMLElement
}

describe('campaign list', () => {
  it('says so when there are no campaigns', async () => {
    mockFetch(
      campaignBackend(
        { campaign: campaign(), review: review() },
        { 'GET /api/v1/campaigns': () => jsonResponse([]) },
      ),
    )
    await renderApp('/campaigns')

    expect(await screen.findByText('No campaigns yet')).toBeVisible()
    expect(screen.getByRole('link', { name: 'New campaign' })).toHaveAttribute(
      'href',
      '/campaigns/new',
    )
  })

  it('shows each campaign with its status, counts and next send', async () => {
    mockFetch(
      campaignBackend(
        { campaign: campaign(), review: review() },
        {
          'GET /api/v1/campaigns': () =>
            jsonResponse([
              summary({
                id: 7,
                name: 'Spring hello',
                status: 'active',
                enrollments: { active: 4, replied: 1 },
                next_action_at: '2030-06-20T12:00:00Z',
              }),
              summary({ id: 5, name: 'Autumn reconnect', enrollments: { pending: 3 } }),
            ]),
        },
      ),
    )
    await renderApp('/campaigns')

    await screen.findByRole('link', { name: 'Spring hello' })
    const active = within(rowOf('Spring hello'))
    expect(active.getByText('Active')).toBeVisible()
    expect(active.getByText('4 active, 1 replied')).toBeVisible()
    expect(active.getByText(/2030/)).toBeVisible()
    const draft = within(rowOf('Autumn reconnect'))
    expect(draft.getByText('Draft')).toBeVisible()
    expect(draft.getByText('3 pending')).toBeVisible()
    expect(draft.getByText('—')).toBeVisible()
    expect(screen.getByRole('link', { name: 'Autumn reconnect' })).toHaveAttribute(
      'href',
      '/campaigns/5',
    )
  })

  it('reports a list that will not load', async () => {
    mockFetch(
      campaignBackend(
        { campaign: campaign(), review: review() },
        { 'GET /api/v1/campaigns': () => jsonResponse({ detail: 'database is locked' }, 500) },
      ),
    )
    await renderApp('/campaigns')

    expect(await screen.findByRole('alert')).toHaveTextContent('database is locked')
  })
})

describe('campaign builder', () => {
  it('saves a draft with every step field and its audience, then opens it', async () => {
    const calls: Call[] = []
    const state = { campaign: campaign(), review: review({ status: 'draft' }) }
    mockFetch(
      campaignBackend(
        state,
        {
          'POST /api/v1/campaigns': () => jsonResponse(state.campaign, 201),
          'GET /api/v1/mailboxes': () =>
            jsonResponse([
              MAILBOX,
              mailbox({ id: 4, email: 'stale@sender.example', status: 'reauth_required' }),
              mailbox({ id: 6, email: 'off@sender.example', status: 'disabled' }),
            ]),
        },
        calls,
      ),
    )
    const { router } = await renderApp('/campaigns/new')

    const save = await screen.findByRole('button', { name: 'Save draft' })
    expect(save).toBeDisabled()
    expect(screen.getByText(/Give the campaign a name/)).toBeVisible()

    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Autumn reconnect' } })
    await screen.findByRole('option', { name: 'me@sender.example' })
    expect(
      screen.getByRole('option', { name: 'stale@sender.example (needs reauth)' }),
    ).toBeDisabled()
    expect(screen.getByRole('option', { name: 'off@sender.example (disabled)' })).toBeDisabled()
    expect(screen.getByRole('option', { name: 'me@sender.example' })).toBeEnabled()
    fireEvent.change(screen.getByLabelText('Mailbox'), { target: { value: '3' } })
    await screen.findAllByRole('option', { name: 'Catching up (email)' })
    const first = within(screen.getByRole('listitem', { name: 'Step 1' }))
    fireEvent.change(first.getByLabelText('Template'), { target: { value: '11' } })

    fireEvent.click(screen.getByRole('button', { name: 'Add a step' }))
    const second = within(screen.getByRole('listitem', { name: 'Step 2' }))
    expect(second.getByLabelText('Delay (days)')).toHaveValue(7)
    expect(second.getByLabelText('Condition')).toHaveValue('no_reply')
    fireEvent.change(second.getByLabelText('Template'), { target: { value: '12' } })
    fireEvent.change(second.getByLabelText('Mode'), { target: { value: 'send' } })
    // An email follow-up after an email starts in its thread; the first step has no box.
    expect(second.getByRole('checkbox')).toBeChecked()
    expect(first.queryByRole('checkbox')).toBeNull()

    fireEvent.change(screen.getByLabelText('Audience from'), { target: { value: 'list' } })
    await screen.findByRole('option', { name: 'Old colleagues (3)' })
    fireEvent.change(screen.getByLabelText('List'), { target: { value: '21' } })

    expect(save).toBeEnabled()
    fireEvent.click(save)

    await waitFor(() => expect(router.state.location.pathname).toBe('/campaigns/5'))
    const post = calls.find((call) => call.method === 'POST' && call.path === '/api/v1/campaigns')
    expect(post?.body).toEqual({
      name: 'Autumn reconnect',
      mailbox_id: 3,
      steps: [
        { template_id: 11, delay_days: 0, mode: 'draft', condition: 'always', same_thread: false },
        { template_id: 12, delay_days: 7, mode: 'send', condition: 'no_reply', same_thread: true },
      ],
      list_id: 21,
    })
  })

  it('offers LinkedIn modes for a LinkedIn step and shows a refusal', async () => {
    mockFetch(
      campaignBackend(
        { campaign: campaign(), review: review() },
        {
          'POST /api/v1/campaigns': () =>
            jsonResponse({ detail: 'a campaign named Autumn reconnect exists' }, 409),
        },
      ),
    )
    await renderApp('/campaigns/new')

    fireEvent.change(await screen.findByLabelText('Name'), {
      target: { value: 'Autumn reconnect' },
    })
    await screen.findAllByRole('option', { name: 'LinkedIn hello (LinkedIn)' })
    const step = within(screen.getByRole('listitem', { name: 'Step 1' }))
    fireEvent.change(step.getByLabelText('Template'), { target: { value: '13' } })
    expect(
      within(step.getByLabelText('Mode'))
        .getAllByRole('option')
        .map((o) => o.textContent),
    ).toEqual(['Prefill (you send it)'])

    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'a campaign named Autumn reconnect exists',
    )
  })

  it('makes a LinkedIn step pick Prefill or Auto-send while auto-send is on (#494)', async () => {
    const calls: Call[] = []
    mockFetch(
      campaignBackend(
        { campaign: campaign(), review: review() },
        {
          'GET /api/v1/campaigns/linkedin/options': () => jsonResponse({ auto_send: true }),
          'POST /api/v1/campaigns': () => jsonResponse(campaign({ id: 9 })),
        },
        calls,
      ),
    )
    await renderApp('/campaigns/new')

    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'Hello' } })
    await screen.findAllByRole('option', { name: 'LinkedIn hello (LinkedIn)' })
    const step = within(screen.getByRole('listitem', { name: 'Step 1' }))
    fireEvent.change(step.getByLabelText('Template'), { target: { value: '13' } })
    const group = within(await step.findByRole('group', { name: 'Mode' }))
    const prefill = group.getByRole('radio', { name: /Prefill \(you send it\)/ })
    const auto = group.getByRole('radio', { name: /Auto-send/ })
    // Both modes, each with what it does, and neither picked for you.
    expect(prefill).not.toBeChecked()
    expect(auto).not.toBeChecked()
    expect(group.getByText(/Waits for you: netkeep types the message/)).toBeVisible()
    expect(group.getByText(/Sends on its own: netkeep types the message/)).toBeVisible()
    const save = screen.getByRole('button', { name: 'Save draft' })
    expect(save).toBeDisabled()
    expect(screen.getByText(/Pick Prefill or Auto-send for each LinkedIn step\./)).toBeVisible()
    expect(step.queryByText(/restricts hardest/)).toBeNull()

    fireEvent.click(auto)
    expect(
      step.getByText(/An automated send is the action LinkedIn restricts hardest/),
    ).toBeVisible()
    expect(step.getByText(/restricts hardest/).textContent).not.toMatch(/ADR/)

    fireEvent.click(prefill)
    expect(step.queryByText(/restricts hardest/)).toBeNull()
    expect(save).toBeEnabled()
    fireEvent.click(save)
    await waitFor(() =>
      expect(calls.some((c) => c.method === 'POST' && c.path === '/api/v1/campaigns')).toBe(true),
    )
    const post = calls.find((c) => c.method === 'POST' && c.path === '/api/v1/campaigns')
    expect((post?.body as { steps: { mode: string }[] }).steps[0]?.mode).toBe('prefill')
  })

  it('keeps an unpicked mode out of the request when the template changes (#494)', async () => {
    mockFetch(
      campaignBackend(
        { campaign: campaign(), review: review() },
        { 'GET /api/v1/campaigns/linkedin/options': () => jsonResponse({ auto_send: true }) },
      ),
    )
    await renderApp('/campaigns/new')

    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'Hello' } })
    await screen.findAllByRole('option', { name: 'LinkedIn hello (LinkedIn)' })
    const step = within(screen.getByRole('listitem', { name: 'Step 1' }))
    fireEvent.change(step.getByLabelText('Template'), { target: { value: '13' } })
    fireEvent.click(
      within(await step.findByRole('group', { name: 'Mode' })).getByRole('radio', {
        name: /Auto-send/,
      }),
    )
    // A new template is a new pick: the old one is not carried over as Auto-send.
    fireEvent.change(step.getByLabelText('Template'), { target: { value: '' } })
    fireEvent.change(step.getByLabelText('Template'), { target: { value: '13' } })
    const group = within(step.getByRole('group', { name: 'Mode' }))
    expect(group.getByRole('radio', { name: /Auto-send/ })).not.toBeChecked()
    expect(screen.getByRole('button', { name: 'Save draft' })).toBeDisabled()
  })

  it("shows a step's template lint findings under the step (#383)", async () => {
    mockFetch(
      campaignBackend(
        { campaign: campaign(), review: review() },
        {
          'GET /api/v1/templates': () =>
            jsonResponse(
              TEMPLATES.map((t) =>
                t.id === 13
                  ? {
                      ...t,
                      lint: [
                        {
                          rule: 'linkedin_newline',
                          severity: 'error',
                          part: 'body',
                          line: 2,
                          field: null,
                          message: 'line 2: the message has a line break',
                        },
                      ],
                    }
                  : t,
              ),
            ),
        },
      ),
    )
    await renderApp('/campaigns/new')

    await screen.findAllByRole('option', { name: 'LinkedIn hello (LinkedIn)' })
    const step = within(screen.getByRole('listitem', { name: 'Step 1' }))
    expect(step.queryByRole('list', { name: 'Step 1 template lint' })).toBeNull()
    fireEvent.change(step.getByLabelText('Template'), { target: { value: '13' } })

    const lint = step.getByRole('list', { name: 'Step 1 template lint' })
    expect(lint).toHaveTextContent('The message has a line break')
    expect(lint).toHaveTextContent('The prefill never presses Enter')
  })
})

describe('campaign detail', () => {
  it('enrolls the audience and shows the skip summary with its reasons', async () => {
    const calls: Call[] = []
    const state = { campaign: campaign(), review: review({ status: 'draft' }) }
    mockFetch(
      campaignBackend(
        state,
        {
          'POST /api/v1/campaigns/5/enroll': () =>
            jsonResponse({
              campaign_id: 5,
              enrolled: 2,
              already: 0,
              excluded: 1,
              removed: 0,
              pending: 2,
              summary: '2 will start, 1 skipped (1 do-not-contact)',
            }),
        },
        calls,
      ),
    )
    await renderApp('/campaigns/5')

    expect(await screen.findByText(/From the list Old colleagues/)).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Enroll the audience' }))

    const outcome = await screen.findByText('2 will start, 1 skipped (1 do-not-contact)', {
      selector: 'p',
    })
    expect(outcome).toBeVisible()
    expect(screen.getByText(/2 enrolled, 0 already in, 1 skipped/)).toBeVisible()
    const post = calls.find((call) => call.path === '/api/v1/campaigns/5/enroll')
    expect(post?.body).toEqual({ confirm: false })
  })

  it('warns that a new source replaces the audience, and sends it', async () => {
    const calls: Call[] = []
    mockFetch(
      campaignBackend(
        { campaign: campaign(), review: review({ status: 'draft' }) },
        {
          'POST /api/v1/campaigns/5/enroll': () =>
            jsonResponse({
              campaign_id: 5,
              enrolled: 4,
              already: 1,
              excluded: 0,
              removed: 2,
              pending: 5,
              summary: '5 will start, none skipped',
            }),
        },
        calls,
      ),
    )
    await renderApp('/campaigns/5')

    fireEvent.click(await screen.findByRole('button', { name: 'Change the source' }))
    expect(screen.getByText('Changing the source replaces the audience.')).toBeVisible()
    await screen.findByRole('option', { name: 'Conference friends (5)' })
    fireEvent.change(screen.getByLabelText('List'), { target: { value: '22' } })
    fireEvent.click(screen.getByRole('button', { name: 'Replace the audience and enroll' }))

    expect(await screen.findByText(/2 removed by the new source/)).toBeVisible()
    const post = calls.find((call) => call.path === '/api/v1/campaigns/5/enroll')
    expect(post?.body).toEqual({ list_id: 22, confirm: false })
  })

  it('shows progress per step, the enrollments, and pauses an active campaign', async () => {
    const calls: Call[] = []
    const active = campaign({
      status: 'active',
      enrollments: { active: 1, replied: 1 },
      steps: STEPS.map((step) => (step.position === 1 ? { ...step, fired: 2, sent: 1 } : step)),
      next_action_at: '2030-06-20T12:00:00Z',
    })
    const state = { campaign: active, review: review({ status: 'active', missing: [] }) }
    mockFetch(
      campaignBackend(
        state,
        {
          'POST /api/v1/campaigns/5/pause': () => {
            state.campaign = { ...state.campaign, status: 'paused' }
            return jsonResponse(state.campaign)
          },
          'POST /api/v1/campaigns/5/resume': () => {
            state.campaign = { ...state.campaign, status: 'active' }
            return jsonResponse(state.campaign)
          },
        },
        calls,
      ),
    )
    await renderApp('/campaigns/5')

    const stepRow = (await screen.findByRole('rowheader', { name: '1' })).closest('tr')
    // Fired, sent, replied, bounced, opted out (#350), and the timing editor's cell (#338).
    await waitFor(() => {
      const cells = within(stepRow as HTMLElement).getAllByRole('cell')
      expect(cells.slice(-6).map((c) => c.textContent)).toEqual(['2', '1', '0', '0', '0', 'Timing'])
    })
    expect(screen.queryByRole('heading', { name: 'Review' })).toBeNull()

    const enrollment = (await screen.findByRole('link', { name: 'Tobias Marrowbone' })).closest(
      'tr',
    )
    expect(within(enrollment as HTMLElement).getByText('Replied')).toBeVisible()
    expect(screen.getByRole('link', { name: 'Rosalind Quillfeather' })).toHaveAttribute(
      'href',
      '/contacts/401',
    )

    fireEvent.click(screen.getByRole('button', { name: 'Pause' }))
    expect(await screen.findByRole('button', { name: 'Resume' })).toBeVisible()
    expect(screen.getByRole('heading', { name: 'Autumn reconnect Paused' })).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }))
    expect(await screen.findByRole('button', { name: 'Pause' })).toBeVisible()
    expect(calls.filter((c) => c.method === 'POST').map((c) => c.path)).toEqual([
      '/api/v1/campaigns/5/pause',
      '/api/v1/campaigns/5/resume',
    ])
  })

  it('searches the enrollments by name and status', async () => {
    const calls: Call[] = []
    mockFetch(
      campaignBackend({ campaign: campaign({ status: 'active' }), review: review() }, {}, calls),
    )
    await renderApp('/campaigns/5')

    fireEvent.change(await screen.findByLabelText('Find an enrollment'), {
      target: { value: 'tobias' },
    })
    fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'replied' } })
    fireEvent.click(screen.getByRole('button', { name: 'Find' }))

    await waitFor(() => {
      // Not the one-row count the filter summary asks for ("of N").
      const last = calls
        .filter((c) => c.path === '/api/v1/campaigns/5/enrollments' && c.query.get('limit') !== '1')
        .at(-1)
      expect(last?.query.get('q')).toBe('tobias')
      expect(last?.query.get('status')).toBe('replied')
    })
  })

  it('shows what narrows the enrollments, how many of how many, and clears it', async () => {
    const enrollments = (call: Call) => {
      const q = call.query.get('q') ?? ''
      const status = call.query.get('status')
      if (q === 'nobody') return jsonResponse({ total: 0, items: [] })
      if (q !== '' || status !== null) {
        return jsonResponse({ total: 1, items: [ENROLLMENTS.items[1]] })
      }
      return jsonResponse({ ...ENROLLMENTS, total: 87 })
    }
    const calls: Call[] = []
    const counts = () =>
      calls.filter(
        (c) => c.path === '/api/v1/campaigns/5/enrollments' && c.query.get('limit') === '1',
      )
    mockFetch(
      campaignBackend(
        { campaign: campaign({ status: 'active' }), review: review() },
        { 'GET /api/v1/campaigns/5/enrollments': enrollments },
        calls,
      ),
    )
    await renderApp('/campaigns/5')
    await screen.findByRole('link', { name: 'Tobias Marrowbone' })
    // Nothing narrows the list yet, so nothing says it does, or asks for "of N".
    expect(screen.queryByRole('list', { name: 'Active filters' })).toBeNull()
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(counts()).toEqual([])

    fireEvent.change(screen.getByLabelText('Find an enrollment'), { target: { value: 'acme' } })
    fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'replied' } })
    fireEvent.click(screen.getByRole('button', { name: 'Find' }))

    await waitFor(() => expect(screen.getByText(/Showing 1 of 87 · filtered by:/)).toBeVisible())
    // The whole count is the unfiltered list, one row of it.
    expect(counts().at(-1)?.query.has('q')).toBe(true)
    expect(counts().at(-1)?.query.get('q')).toBe('')
    expect(counts().at(-1)?.query.has('status')).toBe(false)
    const chips = within(screen.getByRole('list', { name: 'Active filters' }))
    expect(chips.getAllByRole('button').map((chip) => chip.textContent)).toEqual([
      '“acme”',
      'status: replied',
    ])

    // One chip removes one filter.
    fireEvent.click(chips.getByRole('button', { name: 'Remove status: replied' }))
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Remove status: replied' })).toBeNull(),
    )
    expect(screen.getByLabelText('Status')).toHaveValue('')
    expect(screen.getByRole('button', { name: 'Remove “acme”' })).toBeVisible()

    // Clear takes the rest, and the box with it.
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
    await waitFor(() => expect(screen.queryByRole('list', { name: 'Active filters' })).toBeNull())
    expect(screen.getByLabelText('Find an enrollment')).toHaveValue('')
  })

  it('still says how many enrollments match when the whole count fails', async () => {
    mockFetch(
      campaignBackend(
        { campaign: campaign({ status: 'active' }), review: review() },
        {
          'GET /api/v1/campaigns/5/enrollments': (call) =>
            call.query.get('limit') === '1'
              ? jsonResponse({ detail: 'the database is locked' }, 500)
              : call.query.get('q') === 'tobias'
                ? jsonResponse({ total: 1, items: [ENROLLMENTS.items[1]] })
                : jsonResponse(ENROLLMENTS),
        },
      ),
    )
    await renderApp('/campaigns/5')
    fireEvent.change(await screen.findByLabelText('Find an enrollment'), {
      target: { value: 'tobias' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Find' }))
    await waitFor(() =>
      expect(screen.getByText(/filtered by:/, { selector: '[role="status"]' })).toHaveTextContent(
        /^Showing 1 · filtered by:/,
      ),
    )
  })

  it('says no enrollment matches the filters, not that there are none', async () => {
    mockFetch(
      campaignBackend(
        { campaign: campaign({ status: 'active' }), review: review() },
        {
          'GET /api/v1/campaigns/5/enrollments': (call) =>
            call.query.get('q') === 'nobody'
              ? jsonResponse({ total: 0, items: [] })
              : jsonResponse(ENROLLMENTS),
        },
      ),
    )
    await renderApp('/campaigns/5')
    fireEvent.change(await screen.findByLabelText('Find an enrollment'), {
      target: { value: 'nobody' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Find' }))

    expect(await screen.findByText('No enrollments match these filters')).toBeVisible()
    expect(screen.queryByText('No enrollments.')).toBeNull()
    await waitFor(() => expect(screen.getByText(/Showing 0 of 2 · filtered by:/)).toBeVisible())

    fireEvent.click(screen.getByRole('button', { name: 'Clear filters' }))
    expect(await screen.findByRole('link', { name: 'Tobias Marrowbone' })).toBeVisible()
    expect(screen.getByLabelText('Find an enrollment')).toHaveFocus()
  })

  it('says so for a campaign that does not exist', async () => {
    mockFetch(
      campaignBackend(
        { campaign: campaign(), review: review() },
        { 'GET /api/v1/campaigns/5': () => jsonResponse({ detail: 'no campaign 5' }, 404) },
      ),
    )
    await renderApp('/campaigns/5')

    expect(await screen.findByText('No such campaign')).toBeVisible()
  })
})

describe('a campaign with a LinkedIn step (#383)', () => {
  const LINKEDIN_STEP = {
    ...STEPS[0]!,
    id: 103,
    channel: 'linkedin' as const,
    mode: 'prefill' as const,
    template_id: 13,
    template_name: 'LinkedIn hello',
    fired: 6,
    sent: 3,
    outbound: { prefilled: 1, sent: 3, stale: 2 },
  }

  it("shows the step's ready, prefilled, sent and stale counts, and the queue", async () => {
    const calls: Call[] = []
    mockFetch(
      campaignBackend(
        {
          campaign: campaign({ status: 'active', steps: [LINKEDIN_STEP], mailbox_id: null }),
          review: review(),
        },
        {
          'GET /api/v1/campaigns/linkedin/ready': () =>
            jsonResponse({ items: [], total: 4, by_step: { '1': 4 } }),
          'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse({ items: [], total: 0 }),
          'GET /api/v1/campaigns/linkedin/options': () => jsonResponse({ auto_send: false }),
          'GET /api/v1/linkedin/status': () =>
            jsonResponse({
              session_flag: null,
              session_flagged_at: null,
              heat_tripped: false,
              armed: false,
              schedule_paused: false,
              running_run_id: null,
              can_start_runs: true,
            }),
        },
        calls,
      ),
    )
    await renderApp('/campaigns/5')

    const counts = await screen.findByLabelText('Step 1 LinkedIn counts')
    await waitFor(() =>
      expect(counts).toHaveTextContent('4 ready · 1 prefilled · 3 sent · 2 stale'),
    )
    expect(screen.getByText('Prefill (you send it)')).toBeVisible()
    // The step says it waits for you (#494).
    expect(screen.getByText('Waits for you to prefill.')).toBeVisible()
    expect(screen.getByRole('heading', { name: 'LinkedIn queue' })).toBeVisible()
    expect(screen.getByRole('heading', { name: 'Waiting for you' })).toBeVisible()
    const ready = calls.filter((c) => c.path === '/api/v1/campaigns/linkedin/ready')
    expect(ready.every((c) => c.query.get('campaign_id') === '5')).toBe(true)
  })

  it.each([
    [false, 'active', true],
    [true, 'active', false],
    [false, 'completed', false],
  ] as const)(
    'says email replies are not watched when no mailbox watches it (watched %s, %s) (#571)',
    async (watched, status, shown) => {
      mockFetch(
        campaignBackend(
          {
            campaign: campaign({
              status,
              steps: [LINKEDIN_STEP],
              mailbox_id: null,
              email_replies_watched: watched,
            }),
            review: review(),
          },
          {
            'GET /api/v1/campaigns/linkedin/ready': () =>
              jsonResponse({ items: [], total: 0, by_step: {} }),
            'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse({ items: [], total: 0 }),
            'GET /api/v1/campaigns/linkedin/options': () => jsonResponse({ auto_send: false }),
            'GET /api/v1/linkedin/status': () =>
              jsonResponse({
                session_flag: null,
                session_flagged_at: null,
                heat_tripped: false,
                armed: false,
                schedule_paused: false,
                running_run_id: null,
                can_start_runs: true,
              }),
          },
        ),
      )
      await renderApp('/campaigns/5')

      expect(await screen.findByText('Recent-contact guard')).toBeVisible()
      const notice = screen.queryByText('Email replies aren’t watched')
      if (shown) {
        expect(notice).toBeVisible()
        expect(screen.getByText(/No mailbox is armed.*Arm a mailbox/)).toBeVisible()
      } else expect(notice).toBeNull()
    },
  )

  it('says to arm the campaign’s own mailbox when that one is disarmed (#571)', async () => {
    mockFetch(
      campaignBackend(
        {
          campaign: campaign({
            status: 'active',
            steps: [LINKEDIN_STEP],
            email_replies_watched: false,
          }),
          review: review(),
        },
        {
          'GET /api/v1/campaigns/linkedin/ready': () =>
            jsonResponse({ items: [], total: 0, by_step: {} }),
          'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse({ items: [], total: 0 }),
          'GET /api/v1/campaigns/linkedin/options': () => jsonResponse({ auto_send: false }),
        },
      ),
    )
    await renderApp('/campaigns/5')

    expect(
      await screen.findByText(/This campaign’s mailbox, me@sender\.example, isn’t armed/),
    ).toBeVisible()
    expect(screen.getByText(/Arm that mailbox in Settings → Gmail/)).toBeVisible()
  })

  it('never says so for an email-only campaign', async () => {
    mockFetch(
      campaignBackend({
        campaign: campaign({ status: 'active', email_replies_watched: false }),
        review: review(),
      }),
    )
    await renderApp('/campaigns/5')

    expect(await screen.findByText('Recent-contact guard')).toBeVisible()
    expect(screen.queryByText('Email replies aren’t watched')).toBeNull()
  })

  it.each([
    [true, 'Sends on its own. netkeep clicks Send itself.'],
    [false, 'Auto-send step, but auto-send is off in Settings, so it waits for you to prefill it.'],
  ])('says whether an auto-send step sends itself (flag %s)', async (on, text) => {
    mockFetch(
      campaignBackend(
        {
          campaign: campaign({
            status: 'active',
            steps: [{ ...LINKEDIN_STEP, mode: 'auto_send' as const }],
            mailbox_id: null,
          }),
          review: review(),
        },
        {
          'GET /api/v1/campaigns/linkedin/ready': () =>
            jsonResponse({ items: [], total: 0, by_step: {} }),
          'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse({ items: [], total: 0 }),
          'GET /api/v1/campaigns/linkedin/options': () => jsonResponse({ auto_send: on }),
          'GET /api/v1/linkedin/status': () =>
            jsonResponse({
              session_flag: null,
              session_flagged_at: null,
              heat_tripped: false,
              armed: false,
              schedule_paused: false,
              running_run_id: null,
              can_start_runs: true,
            }),
        },
      ),
    )
    await renderApp('/campaigns/5')

    expect(await screen.findByText(text)).toBeVisible()
    expect(screen.getByText('Auto-send')).toBeVisible()
  })

  const LINKEDIN_ROUTES = {
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
    'GET /api/v1/linkedin/budget': () =>
      jsonResponse({
        budgets: [
          { action: 'li_prefills', day: { count: 2, limit: 12, remaining: 10 }, week: null },
          { action: 'li_messages_auto', day: { count: 0, limit: 8, remaining: 8 }, week: null },
        ],
      }),
  }

  function readyItem(overrides: Record<string, unknown> = {}) {
    return {
      enrollment_id: 31,
      campaign_id: 5,
      campaign_name: 'Autumn reconnect',
      step_position: 1,
      contact_id: 41,
      contact_name: 'Rosalind Quillfeather',
      due: '2020-01-02T15:00:00Z',
      held_until: null,
      auto_send: false,
      ...overrides,
    }
  }

  it.each([
    ['prefill' as const, 'li_prefills', false, 'your LinkedIn budget: 12 prefills a day'],
    [
      'auto_send' as const,
      'li_messages_auto',
      true,
      'your LinkedIn budget: 8 auto-sent messages a day',
    ],
    ['auto_send' as const, 'li_prefills', false, 'your LinkedIn budget: 12 prefills a day'],
  ])(
    'gives a LinkedIn-only campaign the budget the API names as its cap (%s, %s)',
    async (mode, budget, on, text) => {
      mockFetch(
        campaignBackend(
          {
            campaign: campaign({
              status: 'active',
              steps: [{ ...LINKEDIN_STEP, mode, budget }],
              mailbox_id: null,
            }),
            review: review(),
          },
          {
            ...LINKEDIN_ROUTES,
            'GET /api/v1/campaigns/linkedin/ready': () =>
              jsonResponse({ items: [], total: 0, by_step: {} }),
            'GET /api/v1/campaigns/linkedin/options': () => jsonResponse({ auto_send: on }),
            'GET /api/v1/linkedin/schedule': () => jsonResponse(SCHEDULE_ARMED),
          },
        ),
      )
      await renderApp('/campaigns/5')

      expect(await screen.findByText(text)).toBeVisible()
      expect(screen.queryByText('the mailbox’s')).toBeNull()
    },
  )

  it('says when a passed auto-send step can go, or why it waits', async () => {
    const until = '2099-01-02T15:40:00Z'
    mockFetch(
      campaignBackend(
        {
          campaign: campaign({
            status: 'active',
            steps: [{ ...LINKEDIN_STEP, mode: 'auto_send' as const }],
            mailbox_id: null,
            next_action_at: '2020-01-02T15:00:00Z',
          }),
          review: review(),
        },
        {
          ...LINKEDIN_ROUTES,
          'GET /api/v1/campaigns/linkedin/ready': () =>
            jsonResponse({ items: [readyItem({ auto_send: true })], total: 1, by_step: {} }),
          'GET /api/v1/campaigns/linkedin/options': () => jsonResponse({ auto_send: true }),
          'GET /api/v1/linkedin/schedule': () =>
            jsonResponse({
              ...SCHEDULE_ARMED,
              auto_send: autoSendOutlook({ wait: 'outside_active_hours', until }),
            }),
        },
      ),
    )
    await renderApp('/campaigns/5')

    expect(
      await screen.findByText(
        `Not sent yet. Sends on its own, but waits. It's outside your LinkedIn active hours. They next open at ${formatWhen(until)}.`,
      ),
    ).toBeVisible()
  })

  it('says a passed prefill step waits for you', async () => {
    mockFetch(
      campaignBackend(
        {
          campaign: campaign({
            status: 'active',
            steps: [LINKEDIN_STEP],
            mailbox_id: null,
            next_action_at: '2020-01-02T15:00:00Z',
          }),
          review: review(),
        },
        {
          ...LINKEDIN_ROUTES,
          'GET /api/v1/campaigns/linkedin/ready': () =>
            jsonResponse({ items: [readyItem()], total: 1, by_step: {} }),
          'GET /api/v1/campaigns/linkedin/options': () => jsonResponse({ auto_send: false }),
          'GET /api/v1/linkedin/schedule': () => jsonResponse(SCHEDULE_ARMED),
        },
      ),
    )
    await renderApp('/campaigns/5')

    expect(
      await screen.findByText(
        'Not sent yet. It waits for you to prefill it in the LinkedIn queue.',
      ),
    ).toBeVisible()
  })

  it('shows no LinkedIn counts or queue for an email-only campaign', async () => {
    mockFetch(campaignBackend({ campaign: campaign({ status: 'active' }), review: review() }))
    await renderApp('/campaigns/5')

    await screen.findByRole('heading', { name: 'Steps' })
    expect(screen.queryByLabelText(/LinkedIn counts/)).toBeNull()
    expect(screen.queryByRole('heading', { name: 'LinkedIn queue' })).toBeNull()
  })
})
