/**
 * Section links (#739): `/settings#gmail` scrolls that section to the top and moves
 * focus to its heading, so a keyboard or screen reader lands where the eye does.
 *
 * The router scrolls to an element that is there when the page renders
 * (`scrollRestoration`, TanStack Router's hash scrolling). Some sections render only
 * once their read answers, so this also waits for the target to appear, for a few
 * seconds at most, and then scrolls to it itself. Focus goes to the section's first
 * heading, made focusable for script only (`tabindex="-1"`), so Tab order is unchanged.
 */
import { useRouter } from '@tanstack/react-router'
import { useEffect } from 'react'

/** How long a link waits for its section to render before it gives up. */
export const HASH_WAIT_MS = 5_000

const HEADINGS = 'h1, h2, h3, h4, h5, h6'

/** Scroll `target` to the top of the view and focus its heading (or itself, with none). */
export function showTarget(target: HTMLElement): void {
  const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false
  // jsdom has no scrollIntoView, hence the optional call.
  target.scrollIntoView?.({ block: 'start', behavior: reduced ? 'auto' : 'smooth' })
  const heading = target.matches(HEADINGS) ? target : target.querySelector<HTMLElement>(HEADINGS)
  const focusable = heading ?? target
  if (!focusable.hasAttribute('tabindex')) focusable.setAttribute('tabindex', '-1')
  focusable.focus({ preventScroll: true })
}

/**
 * Show the element `hash` names, now if it is there, else as soon as it appears, for
 * `HASH_WAIT_MS` at most. Returns the function that stops waiting.
 */
export function watchFor(hash: string): () => void {
  if (hash === '') return () => {}
  const found = document.getElementById(hash)
  if (found !== null) {
    showTarget(found)
    return () => {}
  }
  const observer = new MutationObserver(() => {
    const target = document.getElementById(hash)
    if (target === null) return
    stop()
    showTarget(target)
  })
  const timer = window.setTimeout(() => stop(), HASH_WAIT_MS)
  function stop(): void {
    observer.disconnect()
    window.clearTimeout(timer)
  }
  observer.observe(document.body, { childList: true, subtree: true })
  return stop
}

/**
 * Show the section the location's hash names after every render the router commits.
 * Following the link you are already on pushes no history entry, but the router still
 * renders again, so a second click on the same chip scrolls and moves focus again.
 * Each navigation stops the previous one's wait, so leaving a page while its section
 * is still loading never pulls you back.
 */
export function useHashTarget(): void {
  const router = useRouter()

  useEffect(() => {
    let stop = watchFor(router.state.location.hash)
    const unsubscribe = router.subscribe('onRendered', (event) => {
      stop()
      stop = watchFor(event.toLocation.hash)
    })
    return () => {
      unsubscribe()
      stop()
    }
  }, [router])
}
