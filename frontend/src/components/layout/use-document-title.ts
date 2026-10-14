import { useQuery } from '@tanstack/react-query'
import { useEffect } from 'react'

import { attentionQuery, documentTitle } from '@/features/attention/api'

/**
 * Keeps the browser tab's title at `(N) netkeeper` while N items need you (#508): the
 * number badges added up, Triage left out (`title_total`). Plain `netkeeper` otherwise,
 * and before the first answer. While the count can't be read, the tab keeps the last
 * known number, as the badges do.
 */
export function useDocumentTitle(): void {
  const attention = useQuery(attentionQuery)
  const title = documentTitle(attention.data)
  useEffect(() => {
    document.title = title
  }, [title])
}
