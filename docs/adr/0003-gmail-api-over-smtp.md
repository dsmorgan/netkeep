# 0003. Gmail API with OAuth instead of SMTP

Date: 2026-09-20

## Status

Accepted

## Context

The campaign engine needs to send email, create drafts the user sends by hand, thread follow-ups into the original conversation, label campaign mail, and detect replies and bounces so follow-ups stop. SMTP with an app password covers sending only. The Gmail API covers all of it but requires the user to create a Google Cloud project and an OAuth client, and a consumer account's OAuth app either lives in Testing mode with 7-day token expiry or is published unverified with a warning screen.

## Decision

Use the Gmail API with the `gmail.modify` scope, refresh tokens in the macOS Keychain. Draft mode uses `drafts.create`; send mode uses `messages.send`; follow-ups reply in-thread; each campaign gets a label; reply detection uses the history API with a threads fallback. netkeeper detects an expired token, pauses email steps, and shows a re-auth banner. The setup guide recommends publishing the OAuth app so tokens persist.

## Consequences

- Reply suppression is automatic, which is the single biggest reduction in weekly manual work compared to the reference workflow's mailing tool.
- Setup takes about fifteen minutes the first time. The guide has to be good.
- The tool holds a broad scope on the mailbox. It never deletes mail, logs every call's purpose, and keeps the token out of the database.
- Non-Gmail providers are out of scope until someone proposes an ADR with a provider abstraction and a reply-detection story.

## Amendments

- 2026-09-27 (#269, P3-07): **A precondition for PostgreSQL or more than one sending process.** A campaign step is never sent twice because the tick claims it, by writing its message `scheduled`, in a writer session, and on SQLite that session begins with `BEGIN IMMEDIATE`, so no other writer can claim the same step at the same time. On PostgreSQL, `mark_for_write` does nothing, and two processes could claim one step. P3-07 added no such index (its one migration, 0021, only adds reconcile state to `messages`). Before any PostgreSQL or multi-process deployment, the claim needs a guard the database enforces. A plain unique index on outbound messages over (enrollment, step) is not enough on its own: a merge moves both contacts' messages onto one enrollment, so one enrollment can hold two messages of a step. No index keyed on the enrollment works, a partial one on the tick's claims included, because the messages a merge moves include claimed ones from both sides (#273 review). Either key the guard on something a merge never rewrites, such as a claim key written once at claim time from the claiming enrollment's id and the step (`<enrollment id at claim>:<step id>`, unique), or have the claim lock the enrollment row (`SELECT ... FOR UPDATE`) and re-check the step inside that transaction.
- 2026-09-30 (#302, CP5 finding 1): **The setup guide now recommends Testing, not publishing.** CP5 found **Publish app** disabled until Branding has a home page URL and a privacy policy URL, which Google's rules want on a domain the user owns. A personal tool rarely has one. The guide (and the Settings setup guide that follows it) keeps the app in Testing with the sending address as its one test user, and accepts the 7-day token expiry: netkeeper already detects it, pauses email steps, and shows the re-authorize banner. Publishing is an optional later step that explains the Branding URLs. netkeeper links to the console and shows the `gcloud` commands for the two steps that have them (create the project, enable the Gmail API); it never runs `gcloud` or drives the console, so no code under `netkeeper/` starts a process (`tests/test_browser_safety.py`).
- 2026-10-14 (#279): **netkeeper deletes nothing in Gmail; a future trash switch follows fixed rules.** Status: Proposed.

  *Decision.* netkeeper deletes and trashes nothing in Gmail, and that stays the default. Its Gmail interface has no delete or trash call, and its only label write adds the campaign's label. A draft netkeeper stops tracking stays in your Drafts, and netkeeper records what happened in its own database. [Gmail drafts netkeeper leaves behind](../gmail-deletes.md) lists every such case and what netkeeper does in each.

  Removing drafts may be worth building later, mainly for drafts that should never be sent: one waiting for a contact who opted out or bounced, one a merge discarded, and test drafts. If it's built, it's a switch, and the switch follows these rules:

  1. **Off by default.** Your mailbox is yours, and a draft disappearing when you didn't ask is the mistake you can't see coming.
  2. **Set per mailbox, for one user, and recorded when changed** (who, when, old and new value). A mailbox is the unit you trust or don't, and the record answers "why is my draft gone?"
  3. **Trash, never delete.** You can restore an item from Trash, so a wrong call can be undone; a Gmail delete is permanent. Where Gmail offers no trash for a draft, the switch leaves the draft; it never falls back to a permanent delete.
  4. **Only drafts netkeeper created, matched by draft id and thread.** The draft id and thread id netkeeper stored must both match what Gmail returns when it's read just before the trash. A draft id alone can follow a canceled Schedule send to a new draft (#278), and you may have moved a draft to another thread (#280). Anything that doesn't match exactly is yours.
  5. **Only drafts.** Never a sent or scheduled message, a reply, or a thread. Those are the record of what you sent and received.
  6. **Only from a state netkeeper recorded, never from a guess about Gmail.** A draft that's missing from Drafts may be scheduled, and a draft that reads as discarded may have been sent (#278, #280). netkeeper's reading can be wrong in both, so it never acts on one.
  7. **Only the cases the switch names.** Each case in the catalogue says whether trashing is ever right. A draft left by a reply is never trashed: you may still want to send it. Adding a case is a change to this amendment.
  8. **Never part of a disconnect.** A disconnect is often you withdrawing access, and netkeeper has no token afterward.
  9. **Every trash is visible.** It logs its purpose like every other call, writes a record without message text, and shows in the UI, so you can find the item in Trash. netkeeper marks its own trash on the message, so the drafts poll doesn't read it as a draft you discarded.
  10. **The switch adds no OAuth scope.** It works within `gmail.modify`; if it can't, it isn't built.

  *Until then.* You clean up by hand. netkeeper has no list of the drafts it leaves behind, so the catalogue proposes one. The list comes before any switch: it's useful on its own, and a switch only automates what the list shows.

  *Consequences.* "It never deletes mail" in this ADR stays true: with the switch on, netkeeper still deletes nothing and trashes only the drafts these rules allow. Building the switch takes a safety review and its own issue, and this amendment's status moves to Accepted only when the maintainer accepts these rules.
