import { useCallback, useEffect, useRef, useState } from 'react'

import { useServerEvent } from '@/features/events/use-server-event'

/** The server event a campaign tick publishes after it sent at least one message (#547). */
export const CAMPAIGN_TICK_EVENT = 'campaign.tick'

/**
 * How long a send batch shows as running. The event arrives after the batch is sent, and
 * the next tick is a minute away, so this is a short blink, not a measure of the send.
 */
export const CAMPAIGN_SENDING_MS = 15_000

/**
 * Whether a campaign send batch just ran (#547): true for a few seconds after each
 * `campaign.tick` event. It is the campaigns' own signal and never changes the LinkedIn
 * state. The Gmail reply poll is not shown: it takes seconds and would only blink.
 */
export function useCampaignSending(): boolean {
  const [sending, setSending] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const onTick = useCallback(() => {
    setSending(true)
    if (timer.current !== null) clearTimeout(timer.current)
    timer.current = setTimeout(() => setSending(false), CAMPAIGN_SENDING_MS)
  }, [])
  useServerEvent(CAMPAIGN_TICK_EVENT, onTick)
  useEffect(
    () => () => {
      if (timer.current !== null) clearTimeout(timer.current)
    },
    [],
  )
  return sending
}
