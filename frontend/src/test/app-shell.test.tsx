import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { renderApp } from './render'

const EXPECTED_HREFS = [
  '/',
  '/contacts',
  '/triage',
  '/lists',
  '/campaigns',
  '/templates',
  '/inbox',
  '/linkedin',
  '/gmail',
  '/imports',
  '/exports',
  '/settings',
]

/** A group's name, from the element its `aria-labelledby` points at. */
function groupName(group: HTMLElement): string {
  const id = group.getAttribute('aria-labelledby') ?? ''
  return document.getElementById(id)?.textContent ?? ''
}

describe('app shell', () => {
  it('renders the twelve navigation links from spec 14.3, in order', async () => {
    await renderApp('/')

    const nav = screen.getByRole('navigation', { name: 'Primary' })
    const links = within(nav).getAllByRole('link')

    expect(links).toHaveLength(12)
    expect(links.map((link) => link.getAttribute('href'))).toEqual(EXPECTED_HREFS)
  })

  it('groups the links under People, Outreach, Sources and System, the Dashboard alone (#739)', async () => {
    await renderApp('/')

    const nav = screen.getByRole('navigation', { name: 'Primary' })
    const groups = within(nav).getAllByRole('group')
    const hrefs = (group: HTMLElement) =>
      within(group)
        .getAllByRole('link')
        .map((link) => link.getAttribute('href'))

    expect(groups.map((group) => [groupName(group), hrefs(group)])).toEqual([
      ['People', ['/contacts', '/triage', '/lists']],
      ['Outreach', ['/campaigns', '/templates', '/inbox']],
      ['Sources', ['/linkedin', '/gmail', '/imports', '/exports']],
      ['System', ['/settings']],
    ])
    // The Dashboard sits above the groups, in none of them.
    const dashboard = within(nav).getByRole('link', { name: 'Dashboard' })
    expect(groups.some((group) => group.contains(dashboard))).toBe(false)
  })

  it('has the theme switch in the sidebar, Auto by default (#666)', async () => {
    await renderApp('/contacts')

    const sidebar = screen.getByRole('navigation', { name: 'Primary' }).closest('aside')
    const group = within(sidebar as HTMLElement).getByRole('group', { name: 'Theme' })
    expect(within(group).getByRole('radio', { name: 'Auto' })).toBeChecked()
  })

  it('puts no painted layer between the color field and the page (#666)', async () => {
    const { container } = await renderApp('/')

    const field = container.querySelector('.nk-field') as HTMLElement
    const shell = field.parentElement as HTMLElement
    expect(shell.firstElementChild).toBe(field)
    // The sidebar is solid on purpose; everything else that spans the page lets the field through.
    const content = screen.getByTestId('shell-content')
    const layers = [shell, content, ...content.querySelectorAll(':scope > header, :scope > main')]
    expect(layers).toHaveLength(4)
    for (const layer of layers) expect(layer.className).not.toMatch(/(^|\s)bg-/)
  })

  it('scrolls the sidebar on its own, so the theme footer never covers a link (#666)', async () => {
    await renderApp('/')

    const nav = screen.getByRole('navigation', { name: 'Primary' })
    const sidebar = nav.closest('aside') as HTMLElement
    // The drawer and, from `sm` up, a one-screen sticky column: each its own scroll container.
    expect(sidebar).toHaveClass('overflow-y-auto', 'sm:sticky', 'sm:top-0', 'sm:h-screen')
    // The nav takes the room between header and footer and scrolls inside it.
    expect(nav).toHaveClass('min-h-0', 'flex-1', 'overflow-y-auto')
    const header = sidebar.firstElementChild as HTMLElement
    expect(header).toHaveClass('h-11', 'shrink-0')
    const footer = screen.getByTestId('sidebar-footer')
    expect(footer).toHaveClass('shrink-0')
    expect(footer).not.toHaveClass('sticky')
    expect(sidebar.lastElementChild).toBe(footer)
  })

  it('marks the current section active', async () => {
    // Every page has landed: /inbox, in P3-11b, was the last placeholder.
    await renderApp('/inbox')

    const nav = screen.getByRole('navigation', { name: 'Primary' })
    expect(within(nav).getByRole('link', { name: 'Inbox' })).toHaveAttribute(
      'data-status',
      'active',
    )
    expect(within(nav).getByRole('link', { name: 'Dashboard' })).not.toHaveAttribute(
      'data-status',
      'active',
    )
  })

  describe('below sm, the sidebar is off-canvas (#181)', () => {
    it('opens from the menu button and moves focus to the first link', async () => {
      await renderApp('/')
      const menu = screen.getByRole('button', { name: 'Open navigation' })
      const sidebar = document.getElementById(menu.getAttribute('aria-controls') ?? '')

      expect(menu).toHaveAttribute('aria-expanded', 'false')
      expect(sidebar).toHaveAttribute('data-open', 'false')
      expect(sidebar).toHaveClass('max-sm:invisible')

      fireEvent.click(menu)

      expect(menu).toHaveAttribute('aria-expanded', 'true')
      expect(sidebar).toHaveAttribute('data-open', 'true')
      expect(sidebar).not.toHaveClass('max-sm:invisible')
      const nav = screen.getByRole('navigation', { name: 'Primary' })
      expect(within(nav).getByRole('link', { name: 'Dashboard' })).toHaveFocus()
    })

    it('closes on Escape and returns focus to the menu button', async () => {
      await renderApp('/')
      const menu = screen.getByRole('button', { name: 'Open navigation' })
      fireEvent.click(menu)

      fireEvent.keyDown(document, { key: 'Escape' })

      expect(menu).toHaveAttribute('aria-expanded', 'false')
      expect(menu).toHaveFocus()
    })

    it('closes from its close button and from the backdrop', async () => {
      await renderApp('/')
      const menu = screen.getByRole('button', { name: 'Open navigation' })

      fireEvent.click(menu)
      fireEvent.click(screen.getByRole('button', { name: 'Close navigation' }))
      expect(menu).toHaveAttribute('aria-expanded', 'false')
      expect(menu).toHaveFocus()

      fireEvent.click(menu)
      fireEvent.click(screen.getByTestId('nav-backdrop'))
      expect(menu).toHaveAttribute('aria-expanded', 'false')
      expect(screen.queryByTestId('nav-backdrop')).not.toBeInTheDocument()
    })

    it('closes once a link is followed', async () => {
      const { router } = await renderApp('/')
      const menu = screen.getByRole('button', { name: 'Open navigation' })
      fireEvent.click(menu)

      const nav = screen.getByRole('navigation', { name: 'Primary' })
      fireEvent.click(within(nav).getByRole('link', { name: 'Settings' }))

      expect(menu).toHaveAttribute('aria-expanded', 'false')
      await waitFor(() => expect(router.state.location.pathname).toBe('/settings'))
    })

    it('is modal while open: a labeled dialog, the rest inert, and Tab wraps inside it (#364 S2)', async () => {
      await renderApp('/')
      const menu = screen.getByRole('button', { name: 'Open navigation' })
      expect(screen.queryByRole('dialog', { name: 'Navigation' })).not.toBeInTheDocument()

      fireEvent.click(menu)

      const drawer = screen.getByRole('dialog', { name: 'Navigation' })
      expect(drawer).toHaveAttribute('aria-modal', 'true')
      expect(screen.getByTestId('shell-content')).toHaveAttribute('inert')

      const close = within(drawer).getByRole('button', { name: 'Close navigation' })
      // The theme switch is last; Tab reaches its checked radio (#666).
      const last = within(drawer).getByRole('radio', { name: 'Auto' })

      last.focus()
      fireEvent.keyDown(document, { key: 'Tab' })
      expect(close).toHaveFocus()

      fireEvent.keyDown(document, { key: 'Tab', shiftKey: true })
      expect(last).toHaveFocus()

      fireEvent.keyDown(document, { key: 'Escape' })
      expect(screen.getByTestId('shell-content')).not.toHaveAttribute('inert')
      expect(menu).toHaveFocus()
    })

    it('ignores Escape while closed', async () => {
      await renderApp('/')
      const menu = screen.getByRole('button', { name: 'Open navigation' })

      fireEvent.keyDown(document, { key: 'Escape' })

      expect(menu).toHaveAttribute('aria-expanded', 'false')
      expect(menu).not.toHaveFocus()
    })
  })
})
