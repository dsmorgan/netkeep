import { act, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { GROUPS } from '@/features/settings/config-fields'

import { jsonResponse, mockFetch } from './fetch'
import { renderApp } from './render'

/** A promise the test resolves when it likes, to hold a read in flight. */
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => {
    resolve = done
  })
  return { promise, resolve }
}

/** A LinkedIn budgets field, so the config card for that group renders. */
const PREFILLS_FIELD = {
  key: 'linkedin.budget.li_prefills_per_day',
  group: 'linkedin_budgets',
  label: 'LinkedIn prefills a day',
  help: 'How many campaign messages netkeep may type.',
  kind: 'int',
  applies: 'now',
  applies_note: 'Applies from the next LinkedIn run.',
  minimum: 1,
  maximum: 50,
  warn_above: 20,
  value: 15,
  default: 15,
  ui_value: null,
  file_value: null,
  source: 'default',
  editable: true,
  locked_reason: null,
  confirm_text: null,
  restart_pending: false,
  stored_unreadable: false,
  warnings: [],
  notes: [],
}

function configBody() {
  return {
    config_path: null,
    log_file: '/data/nk/logs/netkeep.log',
    config_changed: false,
    config_error: null,
    restart_pending: false,
    can_restart: true,
    busy: [],
    restart_waiting: false,
    fields: [PREFILLS_FIELD],
  }
}

/** Every read 404s but the ones a test routes: the anchors are there whatever loads. */
function serve(routes: Record<string, () => Response | Promise<Response>> = {}) {
  mockFetch((request) => {
    const route = routes[new URL(request.url).pathname]
    return route === undefined ? new Response('not found', { status: 404 }) : route()
  })
}

let scrolled: Element[] = []

beforeEach(() => {
  scrolled = []
  // jsdom has no scrollIntoView; record which element each call scrolls.
  Element.prototype.scrollIntoView = vi.fn(function (this: Element) {
    scrolled.push(this)
  })
})

afterEach(() => {
  delete (Element.prototype as Partial<Element>).scrollIntoView
})

describe('section anchors (#739)', () => {
  it('gives each Settings section an id', async () => {
    serve()
    await renderApp('/settings')
    for (const id of [
      'gmail',
      'sending-hours',
      'about-you',
      'backups',
      'login-item',
      'do-not-send',
      'posture',
      'support',
    ]) {
      expect(document.getElementById(id), id).not.toBeNull()
    }
  })

  it('gives each config group its own id, the LinkedIn budgets among them', () => {
    const anchors = GROUPS.map((group) => group.anchor)
    expect(anchors).toContain('linkedin-budget')
    expect(new Set(anchors).size).toBe(anchors.length)
  })

  it('gives the LinkedIn page session, connections, inbox, schedule and runs', async () => {
    serve()
    await renderApp('/linkedin')
    for (const id of ['session', 'connections', 'inbox', 'schedule', 'runs']) {
      expect(document.getElementById(id), id).not.toBeNull()
    }
  })

  it('scrolls /settings#gmail to its section and moves focus to the section’s heading', async () => {
    serve()
    await renderApp('/settings#gmail')
    const section = document.getElementById('gmail')
    const heading = screen.getByRole('heading', { level: 2, name: 'Gmail' })
    await waitFor(() => expect(heading).toHaveFocus())
    expect(section?.contains(heading)).toBe(true)
    expect(scrolled).toContain(section)
    // Focusable by script only, so the Tab order does not change.
    expect(heading).toHaveAttribute('tabindex', '-1')
  })

  it('does the same on the LinkedIn page', async () => {
    serve()
    await renderApp('/linkedin#runs')
    const heading = screen.getByRole('heading', { level: 2, name: 'Runs' })
    await waitFor(() => expect(heading).toHaveFocus())
    expect(scrolled).toContain(document.getElementById('runs'))
  })

  it('waits for a section that renders only once its read answers', async () => {
    const config = deferred<Response>()
    serve({ '/api/v1/settings/config': () => config.promise })
    await renderApp('/settings#linkedin-budget')
    expect(document.getElementById('linkedin-budget')).toBeNull()

    await act(async () => {
      config.resolve(jsonResponse(configBody()))
      await config.promise
    })
    const heading = await screen.findByRole('heading', { level: 2, name: 'LinkedIn budgets' })
    await waitFor(() => expect(heading).toHaveFocus())
    expect(scrolled).toContain(document.getElementById('linkedin-budget'))
  })

  it('leaves focus alone on a page link without a hash', async () => {
    serve()
    await renderApp('/settings')
    expect(screen.getByRole('heading', { level: 2, name: 'Gmail' })).not.toHaveFocus()
    expect(scrolled).toEqual([])
  })
})
