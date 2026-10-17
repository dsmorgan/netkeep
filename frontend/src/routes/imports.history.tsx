import { createFileRoute } from '@tanstack/react-router'

import { HistoryImportPage } from '@/features/history-import/history-import-page'

export const Route = createFileRoute('/imports/history')({
  component: HistoryImportPage,
})
