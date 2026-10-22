/**
 * An element's z-index as its Tailwind class sets it (`z-50`, `z-[1]`), or 0
 * for none (`auto`). jsdom paints nothing, so a test that something sits above
 * the page compares these instead (#749).
 */
export function zIndexOf(element: Element): number {
  const match = /(?:^|\s)z-(?:\[(\d+)\]|(\d+))(?:\s|$)/.exec(element.getAttribute('class') ?? '')
  return match ? Number(match[1] ?? match[2]) : 0
}
