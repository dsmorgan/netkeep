import { Outlet, createRootRoute } from '@tanstack/react-router'

import { NotFound as NotFoundPage } from '@/components/not-found'
import { AppShell } from '@/components/layout/app-shell'
import { EventStreamProvider } from '@/features/events/event-stream-provider'

export const Route = createRootRoute({
  component: RootLayout,
  notFoundComponent: NotFound,
})

function RootLayout() {
  return (
    <EventStreamProvider>
      <AppShell>
        <Outlet />
      </AppShell>
    </EventStreamProvider>
  )
}

function NotFound() {
  return (
    <NotFoundPage to="/" backLabel="Back to the dashboard">
      There is no page at this address.
    </NotFoundPage>
  )
}
