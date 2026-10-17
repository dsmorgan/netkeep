import { createFileRoute } from '@tanstack/react-router'

import { CampaignBuilderPage } from '@/features/campaigns/campaign-builder-page'

/** `list`: start with that list as the audience, as "Start a campaign from it" links (#513). */
export interface NewCampaignSearch {
  list?: number
}

function listOf(value: unknown): number | undefined {
  const n = typeof value === 'string' ? Number(value) : value
  return typeof n === 'number' && Number.isInteger(n) && n > 0 ? n : undefined
}

export const Route = createFileRoute('/campaigns/new')({
  validateSearch: (search: Record<string, unknown>): NewCampaignSearch => {
    const list = listOf(search.list)
    return list === undefined ? {} : { list }
  },
  component: NewCampaign,
})

function NewCampaign() {
  // Checked again: the router keeps a raw value the validator left out (#350).
  const list = listOf(Route.useSearch().list)
  return <CampaignBuilderPage initialListId={list ?? null} />
}
