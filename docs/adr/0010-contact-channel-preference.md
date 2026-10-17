# 0010. Contact channel preference: you set it, a campaign skips a mismatch unless you override, and routing comes later

Date: 2026-10-17

## Status

Accepted (#507, 2026-10-17).

The maintainer's answers of 2026-10-17 are folded into the rules below. Per-contact routing is deferred to #585 (Phase 7), which must meet rules 6 to 9.

## Context

Each campaign step sends on one channel, and a campaign can already mix channels step by step (spec 11.2). Some contacts distrust a campaign email and answer on LinkedIn instead. The maintainer wants to record each contact's preferred channel and, later, one campaign that reaches each contact first on it (#507). Today:

- The guards (`netkeeper/services/campaign_guards.py`) run at enrollment and at every fire. The address guard checks only the step's channel. `not_disconnected` excludes a former LinkedIn connection on every channel, as the maintainer kept in #485 (answer 3). A contact who fails a guard at enrollment isn't enrolled at all. "In another campaign" and "contacted recently" already ignore channel: the second reads any outbound interaction or sent campaign message. You can override "contacted recently" for a contact (#446).
- `check_channel` covers a mailbox's cap and reauth state and the browser's heat and budgets (`netkeeper/services/budgets.py`, `netkeeper/services/heat.py`).
- Reply detection (`netkeeper/services/campaign_replies.py`): a reply on either channel after an enrollment's first send on any channel ends it. The Gmail poll watches email replies only while a mailbox is armed (#571); otherwise a campaign with a LinkedIn step shows "Email replies aren't watched" (#572).

## Decision

Each contact gets a preferred channel: **Email**, **LinkedIn**, or **No preference** (the default). You set it; netkeeper only suggests it. For now, a single-channel campaign skips a contact who prefers the other channel, and you can override that skip per contact, as you can for "contacted recently". Per-contact routing ("Preferred channel" steps) is later work, tracked in #585 (Phase 7). Design detail goes in spec 11 with the implementation issues.

### Now: the preference, suggestions, and skip with override

1. **You set the preference; evidence only suggests.** By hand on the contact page, in bulk from the list, through the CLI, and in CSV import. A reply on the other channel produces a suggestion you accept or dismiss. netkeeper never sets a preference automatically: a silent change is the mistake you can't see coming.
2. **The preference records who set it, when, and how** (by hand, by import, or by accepting a suggestion, with the suggestion's evidence). That answers "why LinkedIn?"
3. **A single-channel campaign skips a mismatch by default, and you can override it.** An email campaign excludes a contact who prefers LinkedIn, and a LinkedIn campaign one who prefers email, with its own reason in the review summary, like the existing exclusions. A contact with no preference is never skipped for it. A message on a channel the contact avoids is worse than none.
4. **The skip is checked at enrollment.** A preference changed after a contact is enrolled doesn't remove them; rule 7 covers what routing does then.
5. **No armed mailbox is required.** Using only LinkedIn, with no email at all, is an accepted mode of operation. While a mailbox is armed, an email reply ends a LinkedIn-only enrollment (#571); while none is, the campaign says "Email replies aren't watched" (#572). The preference changes neither.

### Later (#585): routing, and the rules it must meet

#585 adds a step choice of **Preferred channel**, with a template for each channel and a campaign default for contacts with no preference. It must define the handling for both preferences and the default, address the complexity of multi-step campaigns with routing, and add checks that no contact gets lost on the first message or on a follow-up. Whatever it designs must meet these rules:

6. **The channel is resolved at enrollment and fixed at the first send.** The review screen shows the split ("12 by email, 5 by LinkedIn, 3 held") from the resolution the engine uses, so what you approve is what sends.
7. **Follow-ups stay on the first message's channel.** A preference changed after the first send doesn't move the enrollment. One changed before it holds the enrollment for your review, because you approved a different split; netkeeper doesn't quietly re-resolve it.
8. **A gap holds, never falls back.** No usable address or profile on the resolved channel holds the enrollment with a reason; you pick the other channel for it or remove it. This includes a contact with no preference whose default channel has no address. A lost connection isn't a gap: that contact stays excluded everywhere (#485).
9. **A channel's health waits; it doesn't reroute.** A mailbox in reauth or at its cap, or a browser over heat or budget, makes the step wait, as today.

### Both

10. **Guards and budgets stay per channel, and "recently messaged" stays across channels.** Email caps count email; prefill and auto-send budgets count LinkedIn. "Contacted recently" and "in another campaign" keep ignoring channel: someone who heard from you yesterday on LinkedIn has heard from you.
11. **The implementation PRs are safety PRs.** The skip PR tests the skip, its override, and that a no-preference contact is never skipped. #585 tests routing, every hold reason, and a cross-channel reply on both polls.

## Consequences

- A single-channel campaign stops writing to a contact on the channel they avoid unless you say so.
- The skip changes what an existing campaign enrolls once preferences are set. The review summary shows each skipped contact, and the override puts them back.
- Suggestions need a small surface of their own, designed in the spec.
- One campaign that reaches each contact on their preferred channel waits for #585, and holds will add work there: you resolve each gap by hand.
- A future automatic setting, or a fallback to the other channel, is a new ADR that supersedes the matching rule here.
