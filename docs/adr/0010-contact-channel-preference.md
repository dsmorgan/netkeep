# 0010. Contact channel preference: you set it, a step routes by it, and a gap holds for review

Date: 2026-10-17

## Status

Proposed (#507). The maintainer confirms the questions at the end before any implementation starts.

## Context

Each campaign step sends on one channel, and a campaign can already mix channels step by step (spec 11.2). Some contacts distrust a campaign email and answer on LinkedIn instead. The maintainer wants one campaign that reaches each contact first on the channel they prefer (#507). Today:

- The guards (`netkeeper/services/campaign_guards.py`) run at enrollment and at every fire. The address guard checks only the step's channel. `not_disconnected` excludes a former LinkedIn connection on every channel, as the maintainer kept in #485 (answer 3). A contact who fails a guard at enrollment isn't enrolled at all. "In another campaign" and "contacted recently" already ignore channel: the second reads any outbound interaction or sent campaign message.
- `check_channel` covers a mailbox's cap and reauth state and the browser's heat and budgets (`netkeeper/services/budgets.py`, `netkeeper/services/heat.py`).
- Reply detection (`netkeeper/services/campaign_replies.py`): a LinkedIn reply after an enrollment's first send on any channel ends it. The Gmail poll used to watch only enrollments with a sent email, so an email reply to a LinkedIn-only enrollment did not stop it; #571 fixed that while a mailbox is armed, and the campaign page says when none is.

## Decision

Each contact gets a preferred channel: **Email**, **LinkedIn**, or **No preference** (the default). You set it; netkeeper only suggests it. A campaign step can choose **Preferred channel** instead of a fixed channel. Such a step carries one template per channel, and the campaign names a default channel for contacts with no preference. netkeeper resolves each enrollment's channel once, shows the split before you approve the campaign, and keeps it. When the resolved channel can't be used for a contact, netkeeper holds that enrollment for your review and never switches channel on its own. Design detail goes in spec 11 with the implementation issues, including the preference's column and filter on the contact list and each enrollment's channel on the campaign page.

The rules that make it acceptable:

1. **You set the preference; evidence only suggests.** By hand on the contact page, in bulk from the list, through the CLI, and in CSV import. A reply on the other channel produces a suggestion you accept or dismiss. A silent change is the mistake you can't see coming.
2. **The preference records who set it, when, and how** (by hand, by import, or by accepting a suggestion, with the suggestion's evidence). That answers "why LinkedIn?"
3. **A preferred-channel step has a template for each channel.** Email needs a subject; LinkedIn has length limits and none. A step missing either can't be approved.
4. **The channel is resolved at enrollment and fixed at the first send.** The review screen shows the split ("12 by email, 5 by LinkedIn, 3 held") from the resolution the engine uses, so what you approve is what sends.
5. **Follow-ups stay on the first message's channel.** Later preferred-channel steps use the enrollment's channel; fixed-channel steps keep theirs. A preference changed after the first send doesn't move the enrollment; one changed before it holds the enrollment for review, because you approved a different split.
6. **A gap holds, never falls back.** No usable address or profile on the resolved channel holds the enrollment with a reason; you pick the other channel for it or remove it. A held enrollment is a new enrollment state, designed in spec 11.3. A lost connection isn't a gap: that contact stays excluded everywhere (#485). A message on a channel the contact avoids is worse than a late one.
7. **A channel's health waits; it doesn't reroute.** A mailbox in reauth or at its cap, or a browser over heat or budget, makes the step wait, as today.
8. **Guards and budgets stay per channel, and "recently messaged" stays across channels.** Email caps count email; prefill and auto-send budgets count LinkedIn. "Contacted recently" and "in another campaign" keep ignoring channel: someone who heard from you yesterday on LinkedIn has heard from you.
9. **A reply on either channel ends the enrollment.** Before routing ships, the Gmail poll must watch an enrollment from its first send on any channel. A preferred-channel campaign therefore needs an armed mailbox even if every contact resolves to LinkedIn.
10. **Single-channel campaigns keep working unchanged, and they warn.** An email-only campaign shows a contact who prefers LinkedIn as a warning in the review summary and still enrolls them. Skipping would change what existing campaigns send without your asking.
11. **The implementation PRs are safety PRs.** Each tests routing, every hold reason, and a cross-channel reply on both polls.

## Consequences

- One campaign reaches each contact on the channel they prefer, and you see who gets what before anything sends.
- Holds add work: you resolve each gap by hand, including no-preference contacts with no address on the default channel.
- The Gmail reply watch must cover LinkedIn-first enrollments before routing lands.
- Suggestions need a small surface of their own, designed in the spec.
- A future automatic setting, or a fallback to the other channel, is a new ADR that supersedes the matching rule here.

## Questions for the maintainer to confirm

1. Suggestions only, never automatic setting (rule 1)?
2. Is **Preferred channel** a step choice, with a campaign default for no-preference contacts (as proposed), or only a campaign-level routing switch for step 1?
3. Should a preference changed before the first send hold for review (rule 5), or quietly re-resolve?
4. Hold rather than fall back, including for contacts with no preference whose default channel has no address (rule 6)?
5. Should single-channel campaigns warn (rule 10) or skip contacts who prefer the other channel?
6. Is requiring an armed mailbox for any preferred-channel campaign acceptable (rule 9)?
7. Should evidence for a suggestion count only replies to campaign messages, or any inbound message on the other channel (rule 1)?
