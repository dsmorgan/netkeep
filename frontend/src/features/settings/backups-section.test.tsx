import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'

import type { Backup, Backups } from './api'
import { humanSize } from './backup-size'
import { BackupsSection } from './backups-section'

function backup(name: string, createdAt: string, size: number, preRestore = false): Backup {
  return {
    name,
    created_at: createdAt,
    size,
    restore_command: `netkeeper restore /data/backups/${name}`,
    pre_restore: preRestore,
  }
}

const OLDER = backup('netkeeper-20261015T030500Z.sqlite3', '2026-10-15T03:05:00Z', 2048)
const NEWER = backup('netkeeper-20261016T031000Z.sqlite3', '2026-10-16T03:10:00Z', 1572864)

function renderSection(initial: Backups, onPost?: () => Response) {
  let state = initial
  let posts = 0
  mockFetch((request) => {
    const { pathname } = new URL(request.url)
    if (pathname === '/api/v1/backups' && request.method === 'GET') return jsonResponse(state)
    if (pathname === '/api/v1/backups' && request.method === 'POST') {
      posts += 1
      if (onPost) return onPost()
      const made = backup('netkeeper-20261017T120000Z.sqlite3', '2026-10-17T12:00:00Z', 4096)
      state = { ...state, backups: [made, ...state.backups] }
      return jsonResponse(state, 201)
    }
    return jsonResponse({ detail: 'unexpected' }, 500)
  })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <BackupsSection />
    </QueryClientProvider>,
  )
  return { posts: () => posts }
}

describe('BackupsSection', () => {
  it('lists each backup with its size and the command that restores it', async () => {
    renderSection({ directory: '/data/backups', available: true, backups: [NEWER, OLDER] })
    expect(
      await screen.findByText('netkeeper restore /data/backups/netkeeper-20261016T031000Z.sqlite3'),
    ).toBeInTheDocument()
    expect(
      screen.getByText('netkeeper restore /data/backups/netkeeper-20261015T030500Z.sqlite3'),
    ).toBeInTheDocument()
    expect(screen.getByText(/1\.5 MB/)).toBeInTheDocument()
    expect(screen.getByText(/2\.0 KB/)).toBeInTheDocument()
    expect(screen.getByText('/data/backups')).toBeInTheDocument()
  })

  it('marks the backup a restore took of the database it replaced', async () => {
    const undo = backup(
      'netkeeper-20261016T120000Z-pre-restore.sqlite3',
      '2026-10-16T12:00:00Z',
      96,
      true,
    )
    renderSection({ directory: '/data/backups', available: true, backups: [undo, OLDER] })
    expect(await screen.findByText(/taken before a restore/)).toBeInTheDocument()
    expect(screen.getAllByText(/taken before a restore/)).toHaveLength(1)
  })

  it('says so when there are none', async () => {
    renderSection({ directory: '/data/backups', available: true, backups: [] })
    expect(await screen.findByText('No backups yet.')).toBeInTheDocument()
  })

  it('backs up now and shows the new backup', async () => {
    const { posts } = renderSection({ directory: '/d', available: true, backups: [OLDER] })
    fireEvent.click(await screen.findByRole('button', { name: 'Back up now' }))
    expect(
      await screen.findByText('netkeeper restore /data/backups/netkeeper-20261017T120000Z.sqlite3'),
    ).toBeInTheDocument()
    expect(posts()).toBe(1)
  })

  it('shows why a backup could not be made', async () => {
    renderSection({ directory: '/d', available: true, backups: [] }, () =>
      jsonResponse({ detail: 'disk full' }, 409),
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Back up now' }))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('disk full'))
  })

  it('offers no button for a database it cannot back up', async () => {
    renderSection({ directory: '/d', available: false, backups: [] })
    expect(await screen.findByText(/not a SQLite file/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Back up now' })).not.toBeInTheDocument()
  })
})

describe('humanSize', () => {
  it('reads bytes the way a person does', () => {
    expect(humanSize(96)).toBe('96 B')
    expect(humanSize(2048)).toBe('2.0 KB')
    expect(humanSize(1572864)).toBe('1.5 MB')
  })
})
