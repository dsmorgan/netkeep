import type { SetupStep } from './setup-api'

/** Invented setup steps for the setup map's tests. */
export function step(key: string, overrides: Partial<SetupStep> = {}): SetupStep {
  return {
    key,
    number: 1,
    title: key,
    state: 'todo',
    next: false,
    needs: [],
    needs_any: [],
    reason: `${key} reason.`,
    target: '/settings',
    optional: false,
    group: null,
    skippable: true,
    skipped: false,
    ...overrides,
  }
}

/** The server's graph (`netkeep/services/setup.py`), states aside. */
export const GRAPH: SetupStep[] = [
  step('about_you', { number: 1, title: 'About you', skippable: false }),
  step('import', { number: 2, title: 'Import contacts', target: '/imports', skippable: false }),
  step('gmail', { number: 3, title: 'Connect Gmail', group: 'gmail' }),
  step('chrome', {
    number: 4,
    title: 'Sign in to LinkedIn',
    target: '/linkedin',
    group: 'linkedin',
  }),
  step('first_sync', {
    number: 5,
    title: 'First sync',
    needs: ['chrome'],
    target: '/linkedin',
    group: 'linkedin',
  }),
  step('first_inbox', {
    number: 6,
    title: 'First inbox read',
    needs: ['chrome'],
    target: '/linkedin',
    group: 'linkedin',
  }),
  step('arm_schedule', {
    number: 7,
    title: 'Schedule runs',
    needs: ['first_sync'],
    target: '/linkedin',
    group: 'linkedin',
  }),
  step('triage', { number: 8, title: 'Triage', needs: ['import'], target: '/triage' }),
  step('build_list', { number: 9, title: 'Build a list', needs: ['import'], target: '/lists' }),
  step('campaign', {
    number: 10,
    title: 'Create a campaign',
    needs: ['import'],
    needs_any: ['gmail', 'chrome'],
    target: '/campaigns',
  }),
  step('login_item', { number: 11, title: 'Run at login (optional)', optional: true }),
]
