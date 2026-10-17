import { useQuery } from '@tanstack/react-query'
import { useEffect } from 'react'

import { attentionQuery } from './api'
import { FAVICON, faviconFor } from './activity'
import { useCampaignSending } from './use-campaign-sending'

/**
 * Gives the tab's icon a dot while a LinkedIn run is in progress (#347) or a campaign
 * send batch just ran (#547), and only then: the counts go in the title (`useDocumentTitle`), not the icon.
 */
export function useFavicon(): void {
  const attention = useQuery(attentionQuery)
  const campaignSending = useCampaignSending()
  const href = faviconFor(attention.data, campaignSending)
  useEffect(() => {
    const link = document.querySelector<HTMLLinkElement>('link[rel="icon"]')
    if (link === null) return
    link.href = href
    // Unmounted (or about to change), the tab goes back to the plain mark.
    return () => {
      link.href = FAVICON
    }
  }, [href])
}
