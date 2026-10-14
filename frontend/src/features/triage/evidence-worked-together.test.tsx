/**
 * "You both worked at ..." on the card (#84), kept apart from the address-book
 * count of shared companies, which says nothing about your own history.
 */

import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { createFakeBackend, type WorkedTogether } from './test-backend'
import { currentName, renderTriage } from './test-render'

async function section(workedTogether: WorkedTogether[]) {
  const backend = createFakeBackend({ contacts: 2 })
  backend.byId(1).workedTogether = workedTogether
  renderTriage({ backend })
  await currentName()
  const heading = await screen.findByRole('heading', { name: 'Employers you share' })
  const block = heading.parentElement
  if (block === null) throw new Error('the section has no container')
  return block
}

describe('worked together on the card', () => {
  it('says so when no employer matches', async () => {
    const block = await section([])
    expect(block).toHaveTextContent('No shared employer found.')
    expect(block).toHaveTextContent('an earlier employer you shared may not show here')
    expect(block).not.toHaveTextContent(/no employer in common/i)
    expect(block).not.toHaveTextContent('You both worked at')
  })

  it('shows the overlapping years of a confirmed match', async () => {
    const block = await section([
      {
        company: 'Harbor Light Co',
        started_on: '2019-03-01',
        ended_on: '2021-06-30',
        confirmed: true,
      },
    ])
    expect(block).toHaveTextContent('You both worked at Harbor Light Co (2019 to 2021)')
  })

  it('shows a single year, and an open window as "from"', async () => {
    const block = await section([
      {
        company: 'Harbor Light Co',
        started_on: '2020-01-01',
        ended_on: '2020-11-01',
        confirmed: true,
      },
      { company: 'Quill Works', started_on: '2022-02-01', ended_on: null, confirmed: true },
    ])
    expect(block).toHaveTextContent('You both worked at Harbor Light Co (2020)')
    expect(block).toHaveTextContent('You both worked at Quill Works (from 2022)')
  })

  it('shows "until" when only the end is known', async () => {
    const block = await section([
      { company: 'Harbor Light Co', started_on: null, ended_on: '2021-06-30', confirmed: true },
    ])
    expect(block).toHaveTextContent('You both worked at Harbor Light Co (until 2021)')
    expect(block).not.toHaveTextContent('dates unconfirmed')
  })

  it('ignores dates on an unconfirmed match', async () => {
    const block = await section([
      {
        company: 'Harbor Light Co',
        started_on: '2019-03-01',
        ended_on: '2021-06-30',
        confirmed: false,
      },
    ])
    expect(block).toHaveTextContent('Harbor Light Co; dates unconfirmed')
    expect(block).not.toHaveTextContent(/\d{4}/)
  })

  it('says the dates are unconfirmed, and shows no years, when they are', async () => {
    const block = await section([
      { company: 'Harbor Light Co', started_on: null, ended_on: null, confirmed: false },
    ])
    expect(block).toHaveTextContent('You both worked at Harbor Light Co; dates unconfirmed')
    expect(block).not.toHaveTextContent(/\d{4}/)
  })

  it('lists several employers, one line each', async () => {
    const block = await section([
      {
        company: 'Harbor Light Co',
        started_on: '2019-03-01',
        ended_on: '2021-06-30',
        confirmed: true,
      },
      { company: 'Quill Works', started_on: null, ended_on: null, confirmed: false },
    ])
    expect(within(block).getAllByRole('listitem')).toHaveLength(2)
  })

  it('keeps the address-book count under its own heading', async () => {
    const block = await section([
      { company: 'Harbor Light Co', started_on: null, ended_on: null, confirmed: false },
    ])
    expect(block).not.toHaveTextContent('other contact')
    expect(
      screen.getByRole('heading', { name: 'Companies you have other contacts at' }),
    ).toBeInTheDocument()
  })
})
