import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { BUDGET, backend, type Call, type Handler } from '@/features/linkedin/test-support'
import { jsonResponse, mockFetch } from '@/test/fetch'

import type { Automation, GmailMove, LinkedInMove } from './api'
import { GmailAutomationBar, LinkedInAutomationBar } from './automation-bar'
import { automation, BLOCKED_REASON, configWith } from './test-support'

/**
 * One bar against a fake backend that keeps the switch's state: each `PUT` answers the
 * state it moved to, unless the test's handler says otherwise.
 */
function renderBar(
  which: 'linkedin' | 'gmail',
  start: Automation = automation(),
  handlers: Record<string, Handler> = {},
) {
  let state = start
  let reads = 0
  const calls: Call[] = []
  mockFetch(
    backend(
      {
        'GET /api/v1/automation': () => {
          reads += 1
          return jsonResponse(state)
        },
        'GET /api/v1/linkedin/budget': () => jsonResponse(BUDGET),
        'GET /api/v1/settings/config': () => jsonResponse(configWith()),
        'PUT /api/v1/automation/linkedin': (call) => {
          const body = call.body as LinkedInMove
          state = {
            ...state,
            linkedin: {
              ...state.linkedin,
              position: body.position,
              depth: body.depth ?? state.linkedin.depth,
            },
          }
          return jsonResponse(state)
        },
        'PUT /api/v1/automation/gmail': (call) => {
          const body = call.body as GmailMove
          state = { ...state, gmail: { ...state.gmail, position: body.position } }
          return jsonResponse(state)
        },
        ...handlers,
      },
      calls,
    ),
  )
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      {which === 'linkedin' ? <LinkedInAutomationBar /> : <GmailAutomationBar />}
    </QueryClientProvider>,
  )
  const puts = () => calls.filter((call) => call.method === 'PUT')
  return { calls, puts, reads: () => reads }
}

async function group(name: 'LinkedIn automation' | 'Gmail automation') {
  return within(await screen.findByRole('radiogroup', { name }))
}

/** The checked stop's text; `hidden`, since a modal dialog hides the page while open. */
function checked(radios: ReturnType<typeof within>): string | null {
  const on = radios
    .getAllByRole('radio', { hidden: true })
    .find((radio: HTMLElement) => radio.getAttribute('aria-checked') === 'true')
  return on?.textContent ?? null
}

describe('the LinkedIn automation bar (#835)', () => {
  it.each([
    ['off', 'Off', 'Leaves LinkedIn alone.'],
    ['manual', 'Manual', 'Runs when you click Start.'],
    ['automatic', 'Automatic', 'Runs on its own, within active hours.'],
  ] as const)('shows %s checked, with its line', async (position, label, line) => {
    renderBar('linkedin', automation({ linkedin: { position } }))
    const bar = await screen.findByRole('region', { name: 'LinkedIn automation' })
    expect(bar).toHaveAttribute('id', 'schedule')
    expect(checked(await group('LinkedIn automation'))).toBe(label)
    expect(within(bar).getByTestId('position-line')).toHaveTextContent(line)
    const box = screen.queryByRole('checkbox', {
      name: 'Also run the weekly full sync and profile refreshes',
    })
    if (position === 'automatic') expect(box).toBeChecked()
    else expect(box).toBeNull()
  })

  it('asks before Automatic on a fresh read, and sends confirm: true', async () => {
    const { puts, reads } = renderBar('linkedin')
    const radios = await group('LinkedIn automation')
    const before = reads()
    fireEvent.click(radios.getByRole('radio', { name: 'Automatic' }))

    const dialog = await screen.findByRole('alertdialog', {
      name: 'Let netkeep use LinkedIn on its own?',
    })
    // Opening the dialog reads the switch again, and nothing moved yet.
    await waitFor(() => expect(reads()).toBeGreaterThan(before))
    expect(puts()).toEqual([])
    expect(checked(radios)).toBe('Manual')

    const confirm = within(dialog).getByRole('button', { name: 'Turn on Automatic' })
    await waitFor(() => expect(confirm).toBeEnabled())
    fireEvent.click(confirm)
    await waitFor(() => expect(checked(radios)).toBe('Automatic'))
    expect(puts().map((call) => call.body)).toEqual([
      { position: 'automatic', depth: 'full', confirm: true },
    ])
  })

  it('holds Confirm while the fresh read is on its way', async () => {
    let count = 0
    let release: (response: Response) => void = () => {}
    renderBar('linkedin', undefined, {
      'GET /api/v1/automation': () => {
        count += 1
        if (count === 1) return jsonResponse(automation())
        return new Promise<Response>((resolve) => {
          release = resolve
        })
      },
    })
    fireEvent.click((await group('LinkedIn automation')).getByRole('radio', { name: 'Automatic' }))
    const dialog = await screen.findByRole('alertdialog')
    const confirm = within(dialog).getByRole('button', { name: 'Turn on Automatic' })
    await waitFor(() => expect(count).toBe(2))
    expect(confirm).toBeDisabled()

    act(() => release(jsonResponse(automation())))
    await waitFor(() => expect(confirm).toBeEnabled())
  })

  it('asks before checking the weekly full sync box', async () => {
    const { puts } = renderBar(
      'linkedin',
      automation({ linkedin: { position: 'automatic', depth: 'quick' } }),
    )
    fireEvent.click(
      await screen.findByRole('checkbox', {
        name: 'Also run the weekly full sync and profile refreshes',
      }),
    )
    expect(
      await screen.findByRole('alertdialog', { name: 'Add the weekly full sync?' }),
    ).toBeInTheDocument()
    expect(puts()).toEqual([])
  })

  it('works from the keyboard: ArrowRight from Manual asks first and sends nothing', async () => {
    const { puts } = renderBar('linkedin')
    const manual = (await group('LinkedIn automation')).getByRole('radio', { name: 'Manual' })
    act(() => manual.focus())
    fireEvent.keyDown(manual, { key: 'ArrowRight' })
    expect(
      await screen.findByRole('alertdialog', { name: 'Let netkeep use LinkedIn on its own?' }),
    ).toBeInTheDocument()
    expect(puts()).toEqual([])
  })

  it('turns Off in one click, with no dialog', async () => {
    const { puts } = renderBar('linkedin')
    const radios = await group('LinkedIn automation')
    fireEvent.click(radios.getByRole('radio', { name: 'Off' }))
    await waitFor(() => expect(checked(radios)).toBe('Off'))
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(screen.getByTestId('position-line')).toHaveTextContent('Leaves LinkedIn alone.')
    expect(puts().map((call) => call.body)).toEqual([{ position: 'off', confirm: false }])
  })

  it('says why the switch couldn’t load, and shows no position', async () => {
    renderBar('linkedin', undefined, {
      'GET /api/v1/automation': () => jsonResponse({ detail: 'boom' }, 500),
    })
    expect(await screen.findByRole('alert')).toHaveTextContent('boom')
    expect(screen.queryByRole('radio')).toBeNull()
  })
})

describe('the Gmail automation bar (#835)', () => {
  it('shows the position with its line', async () => {
    renderBar('gmail', automation({ gmail: { position: 'off' } }))
    const bar = await screen.findByRole('region', { name: 'Gmail automation' })
    expect(checked(await group('Gmail automation'))).toBe('Off')
    expect(within(bar).getByTestId('position-line')).toHaveTextContent('Leaves Gmail alone.')
    expect(within(bar).queryByText(/isn’t available/)).toBeNull()
  })

  it('asks before Automatic on a fresh read, and sends confirm: true', async () => {
    const { puts, reads } = renderBar('gmail')
    const radios = await group('Gmail automation')
    const before = reads()
    fireEvent.click(radios.getByRole('radio', { name: 'Automatic' }))

    const dialog = await screen.findByRole('alertdialog', {
      name: 'Let netkeep send email on its own?',
    })
    await waitFor(() => expect(reads()).toBeGreaterThan(before))
    expect(puts()).toEqual([])

    const confirm = within(dialog).getByRole('button', { name: 'Turn on Automatic' })
    await waitFor(() => expect(confirm).toBeEnabled())
    fireEvent.click(confirm)
    await waitFor(() => expect(checked(radios)).toBe('Automatic'))
    expect(puts().map((call) => call.body)).toEqual([{ position: 'automatic', confirm: true }])
  })

  it('disables Automatic while the server says it isn’t ready, and says why', async () => {
    const { puts } = renderBar(
      'gmail',
      automation({ gmail: { automatic_ready: false, blocked_reason: BLOCKED_REASON } }),
    )
    const radios = await group('Gmail automation')
    const stop = radios.getByRole('radio', { name: 'Automatic' })
    expect(stop).toHaveAttribute('data-disabled')
    expect(stop).toHaveAccessibleDescription(expect.stringContaining(BLOCKED_REASON))
    expect(screen.getByText(`Automatic isn’t available: ${BLOCKED_REASON}.`)).toBeInTheDocument()

    fireEvent.click(stop)
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(puts()).toEqual([])
  })

  it('moves left without confirm', async () => {
    const { puts } = renderBar('gmail')
    const radios = await group('Gmail automation')
    fireEvent.click(radios.getByRole('radio', { name: 'Off' }))
    await waitFor(() => expect(checked(radios)).toBe('Off'))
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(puts().map((call) => call.body)).toEqual([{ position: 'off', confirm: false }])
  })
})
