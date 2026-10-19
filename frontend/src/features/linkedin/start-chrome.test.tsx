import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { EventStreamContext } from '@/features/events/event-stream-context'
import { FakeEventSource } from '@/test/fake-event-source'
import { jsonResponse, mockFetch } from '@/test/fetch'

import { StartChrome } from './start-chrome'
import { LAUNCH_WAIT_MS, type LaunchEvent } from './start-chrome-copy'
import { BROWSER_HEALTH, backend, type Call, type Handler } from './test-support'
import type { BrowserHealth } from './types'

const START = 'POST /api/v1/linkedin/browser/start'
const HEALTH = 'GET /api/v1/linkedin/browser/health'

function renderButton(
  health: Partial<BrowserHealth> = {},
  handlers: Record<string, Handler> = {},
  compact = false,
) {
  const calls: Call[] = []
  mockFetch(
    backend(
      {
        [HEALTH]: () => jsonResponse({ ...BROWSER_HEALTH, ...health }),
        [START]: () => jsonResponse({ task_id: 'task-1' }, 202),
        ...handlers,
      },
      calls,
    ),
  )
  const source = new FakeEventSource('/api/v1/events')
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <EventStreamContext value={{ status: 'connected', source: source as unknown as EventSource }}>
        <StartChrome compact={compact} />
      </EventStreamContext>
    </QueryClientProvider>,
  )
  const starts = () => calls.filter((call) => call.method === 'POST')
  return { ...utils, calls, source, starts }
}

function launch(overrides: Partial<LaunchEvent> = {}): LaunchEvent {
  return {
    task_id: 'task-1',
    status: 'started',
    refusal: null,
    message: 'started netkeep’s Chrome on port 9333',
    pid: 31,
    ...overrides,
  }
}

async function press(source: FakeEventSource, starts: () => Call[], event: LaunchEvent | null) {
  fireEvent.click(await screen.findByRole('button', { name: 'Start Chrome' }))
  await waitFor(() => expect(starts()).toHaveLength(1))
  await screen.findByRole('button', { name: 'Starting Chrome…' })
  if (event !== null) act(() => source.emit('browser.launch', event))
}

afterEach(() => {
  vi.useRealTimers()
})

describe('StartChrome', () => {
  it('says netkeep’s Chrome isn’t running and offers the button', async () => {
    renderButton()
    expect(await screen.findByText("netkeep's Chrome isn't running.")).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Start Chrome' })).toBeEnabled()
  })

  it('says it is running, with its pid, from the health read', async () => {
    renderButton({ chrome_running: true, chrome_pid: 4242 })
    expect(await screen.findByText(/netkeep's Chrome is running \(pid 4242\)/)).toBeInTheDocument()
  })

  it('is not offered when this process can’t start Chrome', async () => {
    const { calls } = renderButton({ can_start_chrome: false })
    await waitFor(() => expect(calls.some((call) => call.path.endsWith('/health'))).toBe(true))
    expect(screen.queryByRole('button', { name: 'Start Chrome' })).not.toBeInTheDocument()
  })

  it('is disabled while a run holds the browser, and says why', async () => {
    const { starts } = renderButton({ browser_held: true })
    const button = await screen.findByRole('button', { name: 'Start Chrome' })
    expect(button).toBeDisabled()
    expect(screen.getByText('A run is using the browser. Start Chrome when it ends.')).toBeVisible()
    fireEvent.click(button)
    expect(starts()).toHaveLength(0)
  })

  it('sends one start, never first_run, and stays disabled until the launch ends', async () => {
    const { source, starts } = renderButton()
    await press(source, starts, null)
    expect(starts()[0]?.body).toEqual({ first_run: false })
    const pending = screen.getByRole('button', { name: 'Starting Chrome…' })
    expect(pending).toBeDisabled()
    fireEvent.click(pending)
    expect(starts()).toHaveLength(1)
    act(() => source.emit('browser.launch', launch()))
    expect(await screen.findByRole('status')).toHaveTextContent(
      "netkeep's Chrome started, with netkeep in its own window",
    )
    expect(screen.getByRole('button', { name: 'Start Chrome' })).toBeEnabled()
  })

  it('ignores a launch event for another task', async () => {
    const { source, starts } = renderButton()
    await press(source, starts, launch({ task_id: 'someone-else' }))
    expect(screen.getByRole('button', { name: 'Starting Chrome…' })).toBeDisabled()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('hears an outcome that arrived before the 202 was read', async () => {
    let answer: (response: Response) => void = () => {}
    const { source, starts } = renderButton(
      {},
      { [START]: () => new Promise<Response>((resolve) => (answer = resolve)) },
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Start Chrome' }))
    await waitFor(() => expect(starts()).toHaveLength(1))
    act(() => source.emit('browser.launch', launch({ status: 'handed_off' })))
    act(() => answer(jsonResponse({ task_id: 'task-1' }, 202)))
    expect(await screen.findByRole('status')).toHaveTextContent(
      'It opened netkeep in a new window.',
    )
  })

  it.each([
    ['handed_off', null, 'status', 'ok', 'It opened netkeep in a new window.'],
    ['shown', null, 'status', 'ok', 'It brought that window to the front.'],
    ['handoff_unconfirmed', null, 'status', 'ok', "couldn't confirm the new window opened"],
    ['failed', null, 'alert', 'bad', "Chrome didn't start. Check that Google Chrome is installed"],
    ['refused', 'not_dedicated', 'alert', 'warn', "Another Chrome is using netkeep's debug port"],
    ['refused', 'profile_without_port', 'alert', 'warn', 'without the debug port'],
    ['refused', 'run_holds_browser', 'alert', 'warn', 'Try again when the run ends.'],
    ['refused', 'remote_address', 'alert', 'warn', 'points at another computer'],
    ['refused', 'invalid_port', 'alert', 'warn', 'has no valid port'],
  ] as const)(
    'says %s (%s) in plain words, with its tint',
    async (status, refusal, role, tone, words) => {
      const { source, starts } = renderButton()
      await press(source, starts, launch({ status, refusal, message: 'the launcher’s words' }))
      const said = await screen.findByRole(role)
      expect(said).toHaveTextContent(words)
      expect(said).toHaveClass('nk-glass', 'nk-tint', `nk-tone-${tone}`)
      if (status !== 'handed_off' && status !== 'shown') {
        expect(said).toHaveTextContent('netkeep said: the launcher’s words')
      }
    },
  )

  it('says a launch already under way (409) plainly', async () => {
    renderButton(
      {},
      { [START]: () => jsonResponse({ detail: "netkeep's Chrome is already starting" }, 409) },
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Start Chrome' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      "netkeep's Chrome is already starting. Wait for it.",
    )
  })

  it('says so when it never hears back', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const { source, starts } = renderButton()
    await press(source, starts, null)
    act(() => vi.advanceTimersByTime(LAUNCH_WAIT_MS))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      "netkeep didn't hear how the launch went",
    )
    expect(screen.getByRole('button', { name: 'Start Chrome' })).toBeEnabled()
  })

  it('compact: shown only while netkeep’s Chrome isn’t running', async () => {
    const { calls, unmount } = renderButton({ chrome_running: true, chrome_pid: 1 }, {}, true)
    await waitFor(() => expect(calls.some((call) => call.path.endsWith('/health'))).toBe(true))
    expect(screen.queryByRole('button', { name: 'Start Chrome' })).not.toBeInTheDocument()
    unmount()
    renderButton({}, {}, true)
    expect(await screen.findByRole('button', { name: 'Start Chrome' })).toBeEnabled()
  })
})
