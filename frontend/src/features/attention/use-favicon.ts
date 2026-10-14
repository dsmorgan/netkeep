import { useQuery } from '@tanstack/react-query'
import { useEffect } from 'react'

import { attentionQuery } from './api'
import { faviconFor } from './activity'

/**
 * Gives the tab's icon a dot while a LinkedIn run is in progress (#347), and only
 * then: the counts go in the title (`useDocumentTitle`), not the icon.
 */
export function useFavicon(): void {
  const attention = useQuery(attentionQuery)
  const href = faviconFor(attention.data)
  useEffect(() => {
    const link = document.querySelector<HTMLLinkElement>('link[rel="icon"]')
    if (link !== null) link.href = href
  }, [href])
}
