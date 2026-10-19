import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { migrateStorageKeys } from './storage-keys'

beforeEach(() => window.localStorage.clear())
afterEach(() => {
  vi.restoreAllMocks()
  window.localStorage.clear()
})

describe('migrateStorageKeys', () => {
  it('moves each netkeeper.* key to its netkeep.* name, once', () => {
    window.localStorage.setItem('netkeeper.theme', 'dark')
    window.localStorage.setItem('netkeeper.dashboard.tab', 'steps')
    window.localStorage.setItem('other.key', 'kept')
    migrateStorageKeys()
    expect(window.localStorage.getItem('netkeep.theme')).toBe('dark')
    expect(window.localStorage.getItem('netkeep.dashboard.tab')).toBe('steps')
    expect(window.localStorage.getItem('netkeeper.theme')).toBeNull()
    expect(window.localStorage.getItem('netkeeper.dashboard.tab')).toBeNull()
    expect(window.localStorage.getItem('other.key')).toBe('kept')
  })

  it('keeps a value already under the new name', () => {
    window.localStorage.setItem('netkeeper.theme', 'dark')
    window.localStorage.setItem('netkeep.theme', 'light')
    migrateStorageKeys()
    expect(window.localStorage.getItem('netkeep.theme')).toBe('light')
    expect(window.localStorage.getItem('netkeeper.theme')).toBeNull()
  })

  it('does nothing a second time', () => {
    window.localStorage.setItem('netkeeper.theme', 'dark')
    migrateStorageKeys()
    window.localStorage.setItem('netkeep.theme', 'light')
    migrateStorageKeys()
    expect(window.localStorage.getItem('netkeep.theme')).toBe('light')
  })

  it('survives blocked storage', () => {
    vi.spyOn(Storage.prototype, 'key').mockImplementation(() => {
      throw new Error('blocked')
    })
    expect(() => migrateStorageKeys()).not.toThrow()
  })
})
