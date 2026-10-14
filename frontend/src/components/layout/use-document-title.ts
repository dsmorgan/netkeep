import { useQuery } from '@tanstack/react-query'
import { useEffect } from 'react'

import { attentionQuery, documentTitle } from '@/features/attention/api'

/**
 * Keeps the browser tab's title at `(N) netkeeper` while N items need you (#508): the
 * number badges added up, Triage left out (`title_total`). Plain `netkeeper` otherwise,
 * and while the count can't be read.
 */
export function useDocumentTitle(): void {
  const attention = useQuery(attentionQuery)
  const title = documentTitle(attention.data)
  useEffect(() => {
    document.title = title
  }, [title])
}
