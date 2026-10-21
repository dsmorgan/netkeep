/**
 * What the dashboard's detail cards fetch, through the generated client. The setup
 * map reads `GET /setup` (`setup-api.ts`); the status chips reuse the LinkedIn,
 * mailbox and attention queries their pages own.
 */
import { queryOptions } from '@tanstack/react-query'

import { api } from '@/api/client'
import type { components } from '@/api/schema'
import { runsQuery } from '@/features/linkedin/api'

// --- at a glance (P3-12) --------------------------------------------------------------

export type NextFirePage = components['schemas']['NextFirePage']
export type NextFire = components['schemas']['NextFireOut']
export type ChangedJobPage = components['schemas']['ChangedJobPage']
export type ChangedJob = components['schemas']['ChangedJobOut']
export type Inbound = components['schemas']['InboundOut']

export const dashboardKeys = {
  all: ['dashboard'] as const,
  nextFires: () => [...dashboardKeys.all, 'next-fires'] as const,
  changedJobs: () => [...dashboardKeys.all, 'changed-jobs'] as const,
  inbound: () => [...dashboardKeys.all, 'inbound'] as const,
}

/**
 * The next campaign steps due, soonest first (`GET /dashboard/next-fires`).
 *
 * The engine moves these every minute and says nothing when it does (there is
 * no server event for a tick), so this polls once a minute, as often as the
 * tick itself runs.
 */
export const nextFiresQuery = queryOptions({
  queryKey: dashboardKeys.nextFires(),
  queryFn: async ({ signal }): Promise<NextFirePage> => {
    const { data, response } = await api.GET('/api/v1/dashboard/next-fires', { signal })
    if (data === undefined) {
      throw new Error(`GET /api/v1/dashboard/next-fires returned ${response.status}`)
    }
    return data
  },
  refetchInterval: 60_000,
  retry: false,
})

/** Contacts whose position changed in the last 30 days (`GET /dashboard/changed-jobs`). */
export const changedJobsQuery = queryOptions({
  queryKey: dashboardKeys.changedJobs(),
  queryFn: async ({ signal }): Promise<ChangedJobPage> => {
    const { data, response } = await api.GET('/api/v1/dashboard/changed-jobs', { signal })
    if (data === undefined) {
      throw new Error(`GET /api/v1/dashboard/changed-jobs returned ${response.status}`)
    }
    return data
  },
  retry: false,
})

/** Inbound messages in the last seven days, and whether replies are detected yet. */
export const inboundQuery = queryOptions({
  queryKey: dashboardKeys.inbound(),
  queryFn: async ({ signal }): Promise<Inbound> => {
    const { data, response } = await api.GET('/api/v1/dashboard/inbound', { signal })
    if (data === undefined) {
      throw new Error(`GET /api/v1/dashboard/inbound returned ${response.status}`)
    }
    return data
  },
  retry: false,
})

/** The newest LinkedIn run, whatever it was: the browser card's last-run line. */
export const lastRunQuery = runsQuery({ limit: 1, offset: 0 })
