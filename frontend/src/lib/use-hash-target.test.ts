import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { HASH_WAIT_MS, watchFor } from './use-hash-target'

/** Lets the MutationObserver deliver what the last DOM change queued. */
const settle = () => Promise.resolve()

function addSection(id: string): HTMLElement {
  const section = document.createElement('section')
  section.id = id
  section.innerHTML = '<h2>Late section</h2>'
  document.body.append(section)
  return section
}

let scrolled: Element[] = []

beforeEach(() => {
  vi.useFakeTimers()
  scrolled = []
  Element.prototype.scrollIntoView = vi.fn(function (this: Element) {
    scrolled.push(this)
  })
})

afterEach(() => {
  vi.useRealTimers()
  delete (Element.prototype as Partial<Element>).scrollIntoView
  document.body.innerHTML = ''
})

describe('watchFor', () => {
  it('shows a section that appears within the wait', async () => {
    watchFor('late')
    vi.advanceTimersByTime(HASH_WAIT_MS - 1)
    const section = addSection('late')
    await settle()
    expect(scrolled).toEqual([section])
    expect(section.querySelector('h2')).toHaveFocus()
  })

  it('gives up after the wait: a section that appears later is neither scrolled to nor focused', async () => {
    watchFor('late')
    vi.advanceTimersByTime(HASH_WAIT_MS)
    const section = addSection('late')
    await settle()
    expect(scrolled).toEqual([])
    expect(section.querySelector('h2')).not.toHaveFocus()
  })

  it('stops when told to, as leaving the page does', async () => {
    const stop = watchFor('late')
    stop()
    addSection('late')
    await settle()
    expect(scrolled).toEqual([])
    expect(vi.getTimerCount()).toBe(0)
  })

  it('shows a section that is already there at once', () => {
    const section = addSection('here')
    watchFor('here')
    expect(scrolled).toEqual([section])
    expect(section.querySelector('h2')).toHaveFocus()
  })
})
