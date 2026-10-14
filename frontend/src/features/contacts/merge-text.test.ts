import { describe, expect, it } from 'vitest'

import { describeMoves } from './merge-text'
import type { MergeMoves } from './types'

const NONE: MergeMoves = {
  emails: { moved: 0, dropped: 0 },
  phones: { moved: 0, dropped: 0 },
  links: { moved: 0, dropped: 0 },
  positions: { moved: 0, dropped: 0 },
  interactions: 0,
  snapshots: 0,
  tags_added: [],
  tags_removed: [],
  lists_added: [],
  enrollments_moved: 0,
  enrollments_combined: 0,
  messages_moved: 0,
  messages_discarded: 0,
  history_rows: 0,
}

describe('describeMoves', () => {
  it('says a discarded draft stays in Gmail and where it is listed (#543)', () => {
    expect(describeMoves({ ...NONE, messages_discarded: 1 })).toEqual([
      '1 unsent campaign message is discarded, so no step goes out twice; its Gmail draft, if it has one, stays in your Drafts and is listed under Leftover drafts on the Gmail page',
    ])
    expect(describeMoves({ ...NONE, messages_discarded: 2 })).toEqual([
      '2 unsent campaign messages are discarded, so no step goes out twice; any Gmail drafts among them stay in your Drafts and are listed under Leftover drafts on the Gmail page',
    ])
  })
})
