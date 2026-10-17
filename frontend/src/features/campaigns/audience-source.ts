import type { Campaign } from './api'

/** Where a campaign's audience comes from, in words. */
export function currentSource(
  campaign: Campaign,
  listName: string | undefined,
  listsLoading = false,
): string {
  if (campaign.source_list_id !== null) {
    // The name arrives with the lists query; until then a bare id would flash.
    if (listName === undefined && listsLoading) return 'the list …'
    return `the list ${listName ?? `#${campaign.source_list_id}`}`
  }
  if (campaign.filter !== null) return 'a filter'
  return 'nothing yet'
}
