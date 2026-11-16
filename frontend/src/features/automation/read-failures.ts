import type { UseQueryResult } from '@tanstack/react-query'

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** A read the dialog needs that failed, with how to ask again. */
export interface Failed {
  key: string
  text: string
  retry: () => void
  retrying: boolean
}

/** "Couldn't load <what>: <detail>" for each read in `reads` that failed. */
export function failures(
  reads: { key: string; what: string; query: UseQueryResult<unknown>; needed: boolean }[],
): Failed[] {
  return reads
    .filter(({ query, needed }) => needed && query.isError)
    .map(({ key, what, query }) => ({
      key,
      text: `Couldn’t load ${what}: ${message(query.error)}`,
      retry: () => void query.refetch(),
      retrying: query.isFetching,
    }))
}
