import { api } from '@/api/client'
import type { components } from '@/api/schema'
import { apiError } from '@/features/imports/api'

export type HistoryImport = components['schemas']['HistoryImportOut']
export type HistoryScan = components['schemas']['HistoryScanOut']

export interface ImportOptions {
  apply: boolean
  createMissing: boolean
}

export interface ScanOptions {
  apply: boolean
  rescan: boolean
  limit: number | null
  mailbox: string | null
}

/**
 * `POST /history/import`: a dry run unless `apply`, the same service as
 * `netkeeper history import`. The workbook goes up as multipart; see
 * `importArchive` in `imports/api.ts` for why the body is cast.
 */
export async function importHistory(file: File, options: ImportOptions): Promise<HistoryImport> {
  const form = new FormData()
  form.append('file', file)
  const { data, error, response } = await api.POST('/api/v1/history/import', {
    params: { query: { apply: options.apply, create_missing: options.createMissing } },
    body: form as unknown as { file: string },
  })
  if (data === undefined) throw apiError(error, response, 'POST /api/v1/history/import')
  return data
}

/** `POST /history/scan-gmail`: read-only against Gmail; a dry run unless `apply`. */
export async function scanGmail(options: ScanOptions): Promise<HistoryScan> {
  const { data, error, response } = await api.POST('/api/v1/history/scan-gmail', {
    body: {
      apply: options.apply,
      rescan: options.rescan,
      limit: options.limit,
      mailbox: options.mailbox,
    },
  })
  if (data === undefined) throw apiError(error, response, 'POST /api/v1/history/scan-gmail')
  return data
}
