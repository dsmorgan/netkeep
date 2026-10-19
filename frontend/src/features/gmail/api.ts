/**
 * What the Gmail page reads that no other endpoint answers (`GET /gmail/activity`,
 * #449): today's sends against each mailbox's cap, and recent email.
 *
 * Read-only, from the database alone: fetching it never calls Gmail. The sends move
 * with the campaign tick and no server event announces them, so this refetches every
 * minute, like the poll status, and at once when a mailbox's status changes.
 */
import { queryOptions } from '@tanstack/react-query'

import { api } from '@/api/client'
import { detailMessage } from '@/api/errors'
import type { components } from '@/api/schema'

export type GmailActivity = components['schemas']['GmailActivityOut']
export type MailboxSends = components['schemas']['MailboxSendsOut']
export type RecentMessage = components['schemas']['RecentMessageOut']
export type LeftoverDrafts = components['schemas']['GmailLeftoverDraftsOut']
export type LeftoverDraft = components['schemas']['GmailLeftoverDraftOut']

export const gmailKeys = {
  all: ['gmail'] as const,
  activity: () => [...gmailKeys.all, 'activity'] as const,
  leftovers: () => [...gmailKeys.all, 'leftover-drafts'] as const,
}

export const gmailActivityQuery = queryOptions({
  queryKey: gmailKeys.activity(),
  queryFn: async ({ signal }): Promise<GmailActivity> => {
    const { data, error, response } = await api.GET('/api/v1/gmail/activity', { signal })
    if (data === undefined) {
      throw new Error(
        detailMessage(error) ?? `GET /api/v1/gmail/activity returned ${response.status}`,
      )
    }
    return data
  },
  refetchInterval: 60_000,
})

/**
 * The drafts netkeep left in Gmail, for you to clean up by hand
 * (`GET /gmail/leftover-drafts`, #543). Read-only, from the database alone; the drafts
 * poll moves them (a draft you send or delete drops off), so this refetches every minute.
 */
export const gmailLeftoverDraftsQuery = queryOptions({
  queryKey: gmailKeys.leftovers(),
  queryFn: async ({ signal }): Promise<LeftoverDrafts> => {
    const { data, error, response } = await api.GET('/api/v1/gmail/leftover-drafts', { signal })
    if (data === undefined) {
      throw new Error(
        detailMessage(error) ?? `GET /api/v1/gmail/leftover-drafts returned ${response.status}`,
      )
    }
    return data
  },
  refetchInterval: 60_000,
})

/**
 * Gmail's search for one message by its Message-ID, in the mailbox's own account. A
 * link, never a request: the app never talks to Gmail from the browser. Without a
 * Message-ID it opens the account's Drafts.
 */
export function gmailDraftUrl(mailbox: string | null, messageId: string | null): string {
  const account = mailbox ? encodeURIComponent(mailbox).replace('%40', '@') : '0'
  const base = `https://mail.google.com/mail/u/${account}/`
  const id = messageId?.trim().replace(/^<|>$/g, '')
  return id ? `${base}#search/${encodeURIComponent(`rfc822msgid:${id}`)}` : `${base}#drafts`
}

/**
 * The posture rows that concern Gmail and campaign sending, by their stable `key`
 * (`netkeep/services/posture.py`). The rest of the report is the LinkedIn
 * extractor's, and stays on Settings, which shows every row.
 */
export const GMAIL_POSTURE_KEYS: readonly string[] = [
  'gmail_reply_poll',
  'sending_hours',
  'next_campaign_send',
  'campaign_templates',
  'send_records',
]
