/**
 * The localStorage keys' rename from `netkeeper.*` to `netkeep.*` (#661).
 *
 * Every key the UI keeps (the theme, the contacts columns, the Chrome banner) moved
 * with the rename. On load, each old key is read once, written
 * under its new name unless that already holds a value, and removed, so a choice made
 * before the rename survives it. Blocked storage leaves things as they are.
 */

export const LEGACY_PREFIX = 'netkeeper.'
export const PREFIX = 'netkeep.'

export function migrateStorageKeys(storage?: Storage): void {
  try {
    const store = storage ?? window.localStorage
    const old: string[] = []
    for (let index = 0; index < store.length; index += 1) {
      const key = store.key(index)
      if (key?.startsWith(LEGACY_PREFIX)) old.push(key)
    }
    for (const key of old) {
      const value = store.getItem(key)
      const renamed = PREFIX + key.slice(LEGACY_PREFIX.length)
      if (value !== null && store.getItem(renamed) === null) store.setItem(renamed, value)
      store.removeItem(key)
    }
  } catch {
    // Storage is blocked or full: the old keys stay for the next load.
  }
}
