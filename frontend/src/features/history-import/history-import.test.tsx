import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { type Call, backend } from '@/features/imports/test-support'
import { jsonResponse, mockFetch, registerFileContent } from '@/test/fetch'
import { renderApp } from '@/test/render'

const IMPORT_REPORT = {
  applied: false,
  sha256: 'abc',
  campaigns: [
    {
      tab: 1,
      name: 'Quorbelle Fictionary spring',
      started_on: '2026-03-01',
      last_batch_on: null,
      listed: 3,
      matched: 2,
      unmatched: 1,
      ambiguous: 0,
      created: 0,
      clicks: 1,
      opens: 2,
      bounces: 0,
      sent_to: 10,
      unlisted: 7,
      new_rows: 2,
      new_interactions: 2,
      warnings: [],
    },
  ],
  skipped: [],
  new_campaigns: 1,
  new_rows: 2,
  new_interactions: 2,
  unmatched: ['nobody@example.test'],
  ambiguous: [],
  unlisted: 7,
}

const SCAN_REPORT = {
  applied: false,
  nothing_to_scan: false,
  scanned: 2,
  nothing_found: 1,
  campaigns: [
    {
      campaign: 'Quorbelle Fictionary spring',
      replies: 1,
      unsubscribes: 0,
      auto: 0,
      bounces: 0,
      by_subject: 0,
      by_subject_unsubscribe: 0,
    },
  ],
  samples: [{ kind: 'reply', addresses: ['ada@example.test'] }],
  subject_samples: [],
  subject_capped: [],
  subject_unsearchable: [],
  no_subject: [],
  subject_search_skipped: false,
  flagged: 1,
  opted_out: 0,
  new_interactions: 1,
  matched_late: 0,
  unconfirmed_notices: 0,
  no_contact: [],
  stopped: null,
  remaining: 0,
  subject_remaining: 0,
}

function workbook(): File {
  return registerFileContent(new File(['x'], 'old.xlsx'), 'x')
}

describe('old-tool history import page', () => {
  it('previews the import, then applies it with the same file', async () => {
    const calls: Call[] = []
    mockFetch(
      backend(
        {
          'POST /api/v1/history/import': (call) =>
            jsonResponse({ ...IMPORT_REPORT, applied: call.query.get('apply') === 'true' }),
        },
        calls,
      ),
    )
    await renderApp('/imports/history')

    fireEvent.change(await screen.findByLabelText('Old tool workbook (.xlsx)'), {
      target: { files: [workbook()] },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Preview the import' }))

    expect(await screen.findByText('Preview only: nothing was written.')).toBeInTheDocument()
    expect(screen.getByText('Quorbelle Fictionary spring')).toBeInTheDocument()
    expect(screen.getByText('nobody@example.test')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Apply the import' }))

    expect(await screen.findByText(/^Imported\./)).toBeInTheDocument()
    const sent = calls.filter((c) => c.path === '/api/v1/history/import')
    expect(sent.map((c) => c.query.get('apply'))).toEqual(['false', 'true'])
  })

  it('previews the scan, applies it, and says when Gmail stopped it', async () => {
    const calls: Call[] = []
    mockFetch(
      backend(
        {
          'POST /api/v1/history/scan-gmail': (call) => {
            const body = call.body as { apply: boolean }
            return jsonResponse(
              body.apply
                ? { ...SCAN_REPORT, applied: true, stopped: 'rateLimitExceeded', remaining: 4 }
                : SCAN_REPORT,
            )
          },
        },
        calls,
      ),
    )
    await renderApp('/imports/history')

    fireEvent.click(await screen.findByRole('button', { name: 'Preview the scan' }))
    expect(await screen.findByText('ada@example.test')).toBeInTheDocument()
    expect(screen.getByText(/1 contact flagged for review/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Apply the scan' }))
    expect(
      await screen.findByText(/Gmail stopped the scan \(rateLimitExceeded\)/),
    ).toBeInTheDocument()
    await waitFor(() =>
      expect(
        calls
          .filter((c) => c.path === '/api/v1/history/scan-gmail')
          .map((c) => (c.body as { apply: boolean }).apply),
      ).toEqual([false, true]),
    )
  })

  it('is reachable from the imports tabs', async () => {
    mockFetch(backend({}))
    await renderApp('/imports')
    expect(await screen.findByRole('link', { name: 'Old-tool history' })).toBeInTheDocument()
  })
})
