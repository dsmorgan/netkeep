/**
 * What needs you (`GET /attention`, #508): the nav counts, the warning flags and the
 * activity state, in one read-only call.
 *
 * Handling an inbox item, a triage decision and an import commit publish no server
 * event, so this polls once a minute, in a background tab too (the tab title shows the
 * count there). It also refetches at once on the server events that move a count or a
 * flag, and after any successful change this tab makes (`useAttentionRefresh`).
 */
import { queryOptions, useQueryClient } from '@tanstack/react-query'
import type { Middleware } from 'openapi-fetch'
import { useCallback, useEffect } from 'react'

import { api } from '@/api/client'
import type { components } from '@/api/schema'
import { useServerEvent } from '@/features/events/use-server-event'

export type Attention = components['schemas']['AttentionOut']
export type AttentionCounts = components['schemas']['AttentionCountsOut']
export type AttentionFlags = components['schemas']['AttentionFlagsOut']

/** A nav item's number badge. */
export type CountKey = 'inbox' | 'dashboard' | 'triage' | 'imports'
/** A nav item's amber dot. */
export type FlagKey = keyof AttentionFlags

export const ATTENTION_EVERY_MS = 60_000

export const attentionKeys = {
  all: ['attention'] as const,
}

export const attentionQuery = queryOptions({
  queryKey: attentionKeys.all,
  queryFn: async ({ signal }): Promise<Attention> => {
    const { data, response } = await api.GET('/api/v1/attention', { signal })
    // A body without its parts (an older backend, a proxy page) is no answer. Like any
    // failed refetch, it leaves the last good answer showing; before the first one the
    // nav has no badges.
    if (data === undefined || typeof data.counts !== 'object' || typeof data.flags !== 'object') {
      throw new Error(`GET /api/v1/attention returned ${response.status} without counts`)
    }
    return normalize(data)
  },
  refetchInterval: ATTENTION_EVERY_MS,
  refetchIntervalInBackground: true,
  retry: false,
})

const COUNT_KEYS = ['inbox', 'dashboard', 'waiting', 'try_again', 'triage', 'imports'] as const
const FLAG_KEYS = ['dashboard', 'gmail', 'linkedin', 'settings'] as const
const STATES = new Set<string>(['running', 'stopped', 'paused', 'armed', 'idle'])

function count(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? Math.floor(value) : 0
}

function flag(value: unknown): string | null {
  return typeof value === 'string' && value !== '' ? value : null
}

/**
 * The answer with every part made safe to show: a count that is not a finite number
 * reads 0, a flag that is not a sentence reads as off, and an unknown activity state as
 * idle. A partial response then shows no stray dots and no empty names.
 */
export function normalize(raw: Attention): Attention {
  const counts = raw.counts as Record<string, unknown>
  const flags = raw.flags as Record<string, unknown>
  const activity = (raw.activity as Partial<Attention['activity']> | null | undefined) ?? {}
  const state =
    typeof activity.state === 'string' && STATES.has(activity.state) ? activity.state : 'idle'
  return {
    checked_at: raw.checked_at,
    counts: Object.fromEntries(
      COUNT_KEYS.map((key) => [key, count(counts[key])]),
    ) as AttentionCounts,
    flags: Object.fromEntries(FLAG_KEYS.map((key) => [key, flag(flags[key])])) as AttentionFlags,
    activity: {
      state,
      label: typeof activity.label === 'string' ? activity.label : '',
      run_id: typeof activity.run_id === 'number' ? activity.run_id : null,
      run_kind: activity.run_kind ?? null,
    },
    title_total: count(raw.title_total),
  }
}

/** The server events that can move a count, a flag or the activity state. */
export const ATTENTION_EVENTS = [
  'run.started',
  'run.finished',
  'mailbox.status',
  'task.finished',
] as const

const UNSAFE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])

/**
 * The `POST`s that only read (a query that does not fit a query string, a preview, a
 * lint): they change nothing, and the contacts table sends one per filter edit.
 */
const READS = ['/query', '/preview', '/lint']

/**
 * Whether a successful request may have changed what `GET /attention` answers: any
 * change this tab makes (handling an inbox item, a discard, Try again, a triage
 * decision, an import commit or delete, a reconnect...), but never a read. Matching
 * every change rather than a list of pages keeps a new mutation from being missed;
 * the cost is one cheap refetch.
 */
export function changesAttention(method: string, url: string): boolean {
  if (!UNSAFE_METHODS.has(method.toUpperCase())) return false
  const path = new URL(url, 'http://netkeeper.invalid').pathname
  if (!path.startsWith('/api/v1/')) return false
  return !READS.some((suffix) => path.endsWith(suffix))
}

/** The number badge's text: the count, capped at "99+". */
export function badgeText(count: number): string {
  return count > 99 ? '99+' : String(count)
}

/** Whether the menu button and the tab need a mark: a number badge but Triage's, or a dot. */
export function needsYou(attention: Attention | undefined): boolean {
  if (attention === undefined) return false
  return attention.title_total > 0 || Object.values(attention.flags).some((flag) => flag !== null)
}

/**
 * The menu button's name at phone width, where the badges sit in the closed drawer:
 * how many items need you when there is a number, or that something does for a dot.
 */
export function menuLabel(attention: Attention | undefined): string {
  if (attention === undefined || !needsYou(attention)) return 'Open navigation'
  const total = attention.title_total
  if (total === 0) return 'Open navigation, something needs you'
  return `Open navigation, ${total} ${total === 1 ? 'item needs' : 'items need'} you`
}

/** The tab title: `(N) netkeeper`, N being `title_total` (Triage stays out). */
export function documentTitle(attention: Attention | undefined): string {
  const total = attention?.title_total ?? 0
  return total > 0 ? `(${total}) netkeeper` : 'netkeeper'
}

/**
 * Refetch `GET /attention` on the server events that move it, and after every
 * successful change this tab makes. The app shell mounts it once.
 */
export function useAttentionRefresh(): void {
  const queryClient = useQueryClient()
  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: attentionKeys.all })
  }, [queryClient])
  useServerEvent(ATTENTION_EVENTS[0], refresh)
  useServerEvent(ATTENTION_EVENTS[1], refresh)
  useServerEvent(ATTENTION_EVENTS[2], refresh)
  useServerEvent(ATTENTION_EVENTS[3], refresh)

  useEffect(() => {
    const middleware: Middleware = {
      onResponse({ request, response }) {
        if (response.ok && changesAttention(request.method, request.url)) refresh()
        return undefined
      },
    }
    api.use(middleware)
    return () => api.eject(middleware)
  }, [refresh])
}
