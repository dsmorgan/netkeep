import type { SetupStep } from './setup-api'

/** Invented setup steps for the Steps tab's tests. */
export function step(key: string, overrides: Partial<SetupStep> = {}): SetupStep {
  return {
    key,
    title: key,
    state: 'todo',
    next: false,
    needs: [],
    needs_any: [],
    reason: `${key} reason.`,
    target: '/settings',
    optional: false,
    skipped: false,
    ...overrides,
  }
}

/** The server's graph (`netkeep/services/setup.py`), states aside. */
export const GRAPH: SetupStep[] = [
  step('about_you'),
  step('import', { target: '/imports' }),
  step('gmail'),
  step('chrome', { target: '/linkedin' }),
  step('first_sync', { needs: ['chrome'], target: '/linkedin' }),
  step('first_inbox', { needs: ['chrome'], target: '/linkedin' }),
  step('triage', { needs: ['import'], target: '/triage' }),
  step('build_list', { needs: ['import'], target: '/lists' }),
  step('campaign', { needs: ['import'], needs_any: ['gmail', 'chrome'], target: '/campaigns' }),
  step('arm_schedule', { needs: ['first_sync'], target: '/linkedin' }),
  step('login_item', { optional: true }),
]
