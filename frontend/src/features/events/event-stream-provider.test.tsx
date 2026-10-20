import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { act, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { FakeEventSource, resetFakeEventSource } from '@/test/fake-event-source'

import { EventStreamProvider } from './event-stream-provider'

afterEach(() => {
  resetFakeEventSource()
  vi.unstubAllGlobals()
})

describe('EventStreamProvider', () => {
  it('refetches every page once the stream comes back, not on the first connect (#704)', async () => {
    vi.stubGlobal('EventSource', FakeEventSource)
    const fetches = { a: 0, b: 0 }
    function Probe({ id }: { id: 'a' | 'b' }) {
      useQuery({
        queryKey: ['probe', id],
        queryFn: () => {
          fetches[id] += 1
          return id
        },
      })
      return <p>{id}</p>
    }
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <EventStreamProvider>
          <Probe id="a" />
          <Probe id="b" />
        </EventStreamProvider>
      </QueryClientProvider>,
    )
    await screen.findByText('a')
    await vi.waitFor(() => expect(fetches).toEqual({ a: 1, b: 1 }))

    // The first connect misses nothing, so it refetches nothing.
    const first = FakeEventSource.instances[0]!
    act(() => first.onopen?.())
    await new Promise((resolve) => setTimeout(resolve, 30))
    expect(fetches).toEqual({ a: 1, b: 1 })

    // A drop, then a reconnect: events may have been missed on any page.
    vi.useFakeTimers()
    act(() => first.onerror?.())
    act(() => {
      vi.advanceTimersByTime(1_000)
    })
    vi.useRealTimers()
    const second = FakeEventSource.instances[1]!
    expect(second).toBeDefined()
    act(() => second.onopen?.())
    await vi.waitFor(() => expect(fetches).toEqual({ a: 2, b: 2 }))
  })
})
