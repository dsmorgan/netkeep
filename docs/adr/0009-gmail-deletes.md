# 0009. netkeeper deletes nothing in Gmail; a future trash switch follows fixed rules

Date: 2026-10-14

## Status

Accepted (#279). Proposed 2026-10-14; accepted by the maintainer the same day.

Builds on [ADR 0003](0003-gmail-api-over-smtp.md), which says netkeeper never deletes mail. This ADR keeps that rule and sets the rules any later removal of drafts must follow.

## Context

netkeeper holds the `gmail.modify` scope on your mailbox. It makes drafts, and in several cases it stops tracking one: a merge discards it, its enrollment ends with a reply, an opt-out or a bounce, or it's a test draft. Each of those drafts stays in your Drafts. Some should never be sent, such as one waiting for a contact who opted out, and nothing in netkeeper can stop you sending it from Gmail.

The maintainer decided on #273 that netkeeper deletes nothing in Gmail for now, and that any removal comes later, behind a switch that's off by default. [Gmail drafts netkeeper leaves behind](../gmail-deletes.md) lists every case, what netkeeper does in each today, and whether trashing would ever be right.

## Decision

netkeeper deletes and trashes nothing in Gmail, and that stays the default. The guarantee rests on the code, not the OAuth scope, since `gmail.modify` would allow a delete: netkeeper's Gmail interface has no delete or trash call, and its one label write only adds the campaign's label. A test pins both. A draft netkeeper stops tracking stays in your Drafts, and netkeeper records what happened in its own database.

If removing drafts is built later, it's a switch, and the switch follows these rules:

1. **Off by default.** Your mailbox is yours, and a draft disappearing when you didn't ask is the mistake you can't see coming.
2. **Set per mailbox, for one user, and recorded when changed** (who, when, old and new value). A mailbox is the unit you trust or don't, and the record answers "why is my draft gone?"
3. **Trash, never delete.** You can restore an item from Trash, so a wrong call can be undone; a Gmail delete is permanent. Where Gmail offers no trash for a draft, the switch leaves the draft; it never falls back to a permanent delete.

   *Open question.* Gmail's API has no trash method for drafts, and deleting a draft is permanent. Trashing a draft would mean trashing the draft's message, and nobody has confirmed what Gmail does then. It needs a live check before any switch is built, and every case the catalogue marks as a trash candidate depends on the answer.
4. **Only drafts netkeeper created, matched by draft id and thread.** The draft id, thread id, and Message-ID netkeeper stored must all match what Gmail returns when it's read just before the trash, and the message must carry no `SENT` or `SCHEDULED` label. A draft id alone can follow a canceled Schedule send to a new draft (#278), and you may have moved a draft to another thread (#280). Anything that doesn't match exactly is yours.
5. **Only drafts.** Never a sent or scheduled message, a reply, or a thread. Those are the record of what you sent and received.
6. **Only from a state netkeeper recorded, never from a guess about Gmail.** A draft that's missing from Drafts may be scheduled, and a draft that reads as discarded may have been sent (#278, #280). netkeeper's reading can be wrong in both, so it never acts on one.
7. **Only the cases the switch names.** Each case in the catalogue says whether trashing is ever right. A draft left by a reply is never trashed: you may still want to send it. Adding a case is an amendment to this ADR.
8. **Never part of a disconnect.** A disconnect is often you withdrawing access, and netkeeper has no token afterward.
9. **Every trash is visible.** It logs its purpose like every other call, writes a record without message text, and shows in the UI, so you can find the item in Trash. netkeeper marks its own trash on the message, so the drafts poll doesn't read it as a draft you discarded.
10. **The switch adds no OAuth scope.** It works within `gmail.modify`; if it can't, it isn't built.

Until a switch exists, you clean up by hand. netkeeper has no list of the drafts it leaves behind, so the catalogue proposes one. The list comes before any switch: it's useful on its own, and a switch only automates what the list shows.

## Consequences

- ADR 0003's "it never deletes mail" stays true: with the switch on, netkeeper still deletes nothing and trashes only the drafts these rules allow.
- Leftover drafts are your job until the list and the switch exist. Opt-out and bounce drafts carry the most risk.
- Building the switch takes its own issue and a safety review against these rules.

## Amendments

- 2026-10-14: the list of leftover drafts exists on the Gmail page (#543); the switch doesn't.
