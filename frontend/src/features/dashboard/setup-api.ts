/**
 * `GET /api/v1/setup` (#651): where first-run setup stands, one step per box on the
 * setup map, with skip and un-skip. The server decides every state and the gating
 * between steps; nothing here recomputes either.
 */
import { queryOptions } from '@tanstack/react-query'

import { api } from '@/api/client'
import { detailMessage } from '@/api/errors'
import type { components } from '@/api/schema'

export type Setup = components['schemas']['SetupOut']
export type SetupStep = components['schemas']['SetupStepOut']
export type SetupStepState = SetupStep['state']

export const setupKeys = {
  all: ['setup'] as const,
}

export const setupQuery = queryOptions({
  queryKey: setupKeys.all,
  queryFn: async ({ signal }): Promise<Setup> => {
    const { data, response } = await api.GET('/api/v1/setup', { signal })
    if (data === undefined) {
      throw new Error(`GET /api/v1/setup returned ${response.status}`)
    }
    return data
  },
  retry: false,
})

/**
 * Skip a step, or clear its skip. Answers the whole setup, as `GET` does. A refusal
 * (409: a step that can't be skipped, or the second of Gmail and LinkedIn) throws the
 * server's own sentence, so the page says why.
 */
export async function setSkipped({ key, skipped }: { key: string; skipped: boolean }) {
  const params = { params: { path: { key } } }
  const { data, error, response } = skipped
    ? await api.POST('/api/v1/setup/steps/{key}/skip', params)
    : await api.DELETE('/api/v1/setup/steps/{key}/skip', params)
  if (data === undefined) {
    throw new Error(
      detailMessage(error) ??
        `${skipped ? 'Skipping' : 'Undoing the skip of'} the step failed (${response.status})`,
    )
  }
  return data
}
