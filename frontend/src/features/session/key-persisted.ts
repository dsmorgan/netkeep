import { createContext, useContext } from 'react'

/**
 * Whether the Keychain holds netkeep's local API key (#618): false when it refused and
 * the key lives in memory until netkeep stops. `SessionGate` provides it from its
 * session check; without a gate (a test rendering part of the app) it reads true.
 */
export const KeyPersistedContext = createContext(true)

export function useKeyPersisted(): boolean {
  return useContext(KeyPersistedContext)
}
