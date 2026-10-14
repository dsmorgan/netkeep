# Gmail drafts netkeeper leaves behind

netkeeper deletes nothing in Gmail ([ADR 0003](adr/0003-gmail-api-over-smtp.md)). The guarantee rests on the code, not the OAuth scope: `gmail.modify` allows `drafts.delete`, and a label change could add `TRASH`. netkeeper's Gmail interface has no delete or trash call (`netkeeper/campaigns/gmail.py:247-310`), and `modify_labels` has one caller, which only adds the campaign's label (`netkeeper/services/campaign_sender.py:720`). `tests/test_gmail_never_deletes.py` pins both. This page lists every case where netkeeper stops caring about something it put in Gmail, so a future switch that trashes drafts can be designed once (#279). The rules that switch must follow are in [ADR 0009](adr/0009-gmail-deletes.md).

Each case says:

- **Today:** what netkeeper does now, with the code that does it.
- **Trash?** Whether trashing the draft would ever be right.
- **By hand:** whether you can see the leftover draft in netkeeper's UI and clean it up yourself.

Every **Yes** under **Trash?** depends on the open question in ADR 0009: whether Gmail can trash a draft at all.

Line numbers are as of `acfe01b5` on `main`.

## Summary

| Case | Today | Trash ever right? | Shown for cleanup in the UI? |
|---|---|---|---|
| 1. A merge discards a waiting draft | Message marked `discarded`; draft stays | Yes, a candidate | Yes, listed |
| 2. A detected reply ends the enrollment | Enrollment `replied`; draft stays `drafted` | No | Yes, listed last |
| 3. A detected opt-out ends the enrollment | Enrollment `opted_out`; draft stays `drafted` | Yes, the strongest candidate | Yes, flagged |
| 4. A bounce ends the enrollment | Enrollment `bounced`; draft stays `drafted` | Yes, a candidate | Yes, flagged |
| 5. You remove an enrollment | Enrollment `removed`; draft stays `drafted` | Maybe, only if you ask | Yes, listed |
| 6. You record an outside response (#512) | Ends like case 2 or 3 | As case 2 or 3 | As case 2 or 3 |
| 7. You mark a contact do not contact, or add an address to do-not-send | Nothing changes until the next step fires | Yes, as case 3 | Yes, flagged |
| 8. A campaign is paused or ended | Campaign status only; drafts stay | No | Partly: the end dialog warns |
| 9. A campaign is archived | Only an ended campaign; drafts stay | No | No |
| 10. A campaign is deleted | Only a never-activated one; its test drafts stay | Yes, a candidate | Yes, listed |
| 11. Review and arming test drafts | Kept; searched for, never touched | Yes, the safest candidate | Yes, listed |
| 12. A mailbox is disconnected or its token expires | Token forgotten; drafts stay `drafted` | No | No |
| 13. Import rollback | Refused if a created contact has any message | Never needed | Not applicable |
| 14. A contact is archived | Contact archived; draft stays `drafted` | No | No |
| 15. Schedule send (#278) | Tracked in Scheduled; stays `drafted` | No | Not applicable |
| 16. A draft edited into a new thread (#280) | Reads as `discarded`; enrollment `removed` | No | Not applicable |
| 17. A step adopts a new template version | Waiting draft keeps its text | No | Partly: adoption preview |
| 18. A LinkedIn step changes mode (#536) | No Gmail draft involved | Not applicable | Not applicable |
| 19. Reconcile gives up on a leftover | `discarded` or `failed`; a draft Gmail made may remain | Yes, a candidate, once confirmed | No |

The Gmail page lists the leftover drafts (#543). See [The cleanup list](#the-cleanup-list).

## The cases

### 1. A merge discards a waiting draft

- **Today:** when two merged contacts both have an enrollment in one campaign, the outranked enrollment's unsent messages are marked `discarded` (`netkeeper/crm/identity.py:1520-1532`; the statuses are at `identity.py:1388-1395`). Only the status changes, so the message keeps its `gmail_draft_id`, and the Gmail draft stays in Drafts. The drafts poll tracks only `drafted` messages (`netkeeper/services/campaign_engine.py:2301-2308`), so netkeeper stops watching that draft, and if you send it anyway, the send may go unrecorded.
- **Trash?** Yes, it's a candidate. netkeeper made the draft, nothing will send it, and sending it would repeat a step the surviving enrollment holds. Trash, not delete, because you may have edited it.
- **By hand:** yes. The merge preview says the discarded drafts stay in Gmail, and the Gmail page lists them under Leftover drafts (#543). Once #544's fix (PR #545) is in, a discarded draft you send is recorded `sent`, and one you delete loses its draft id, so either drops off this list.

### 2. A detected reply ends the enrollment

- **Today:** the reply poll ends the enrollment `replied` (`_after_reply`, `netkeeper/services/campaign_replies.py:640-677`), and ending an enrollment changes no message (`netkeeper/services/campaign_engine.py:872-891`). The waiting draft stays `drafted`, and the drafts poll still records it `sent` if you send it.
- **Trash?** No. You may want to edit the draft and send it as your answer.
- **By hand:** yes. Leftover drafts lists it last, since you may still want to send it.

### 3. A detected opt-out ends the enrollment

- **Today:** an unsubscribe reply sets the contact `do_not_contact`, puts the address on the do-not-send list, and ends the enrollment `opted_out` (`netkeeper/services/campaign_replies.py:55-57`, `:653-673`). The draft stays `drafted`.
- **Trash?** Yes, the strongest candidate. Sending that draft by hand would ignore the opt-out, and nothing in netkeeper can stop a send you make in Gmail.
- **By hand:** yes. Leftover drafts lists it first and flags it, and the Gmail nav item shows a dot.

### 4. A bounce ends the enrollment

- **Today:** the enrollment ends `bounced` (`netkeeper/services/campaign_replies.py:710`) and the address goes on the do-not-send list. The draft stays `drafted`.
- **Trash?** Yes, a candidate. The draft is addressed to an address that bounced.
- **By hand:** yes, flagged, as in case 3.

### 5. You remove an enrollment

- **Today:** `remove_enrollment` ends it `removed` and changes no message (`netkeeper/services/campaign_engine.py:859-869`). The draft stays `drafted`.
- **Trash?** Maybe, but only as a choice you make in the removal itself. You removed the contact from the sequence, which doesn't say what you want done with a draft you may have edited.
- **By hand:** yes. Leftover drafts lists it.

### 6. You record an outside response (#512)

- **Today:** a response you record (a call, a text, in person) ends every live enrollment of the contact through `_after_reply`, the path a detected reply takes (`netkeeper/services/campaign_replies.py:640-677`), or as an opt-out when you say so. A `pending` enrollment, which detection never sees, ends the same way (`netkeeper/services/outside_responses.py:33-40`, `:272-283`). Drafts stay `drafted`.
- **Trash?** As case 2 for a response, and as case 3 for an opt-out.
- **By hand:** yes, as case 2 or 3. The record-response dialog doesn't mention waiting drafts.

### 7. You mark a contact do not contact, or add an address to do-not-send

- **Today:** nothing changes at once. The guards check at the next step fire (`netkeeper/crm/do_not_send.py:1-6`; `netkeeper/services/campaign_guards.py:262`, `:300`), and a do-not-contact contact's enrollment then ends `opted_out` (`netkeeper/services/campaign_engine.py:217-220`). A draft step's next step is scheduled only after the draft is seen sent, so while the draft waits, no step fires and nothing checks.
- **Trash?** Yes, as case 3.
- **By hand:** yes. A draft still waiting for a contact who is do not contact, or who has an address on the do-not-send list, is listed under Leftover drafts and flagged, though its enrollment is live.

### 8. A campaign is paused or ended

- **Today:** pausing or ending changes the campaign's status only (`netkeeper/services/campaign_engine.py:778-787`, `:809-829`). The drafts poll keeps tracking its drafts, so a draft you send is still recorded.
- **Trash?** No. A paused campaign may resume, and an ended one's drafts are yours to send.
- **By hand:** partly. The end dialog says "a Gmail draft stays yours to send or delete" (`frontend/src/features/campaigns/campaign-detail-page.tsx:361-362`), but doesn't list the drafts.

### 9. A campaign is archived

- **Today:** only an ended (`completed`) campaign can be archived, and archiving changes its status only (`netkeeper/services/campaigns.py:953-980`).
- **Trash?** No. Archiving hides a campaign; it isn't a request to clean up mail, and you can unarchive it.
- **By hand:** no. The archived callout says the campaign's messages stay (`campaign-detail-page.tsx:289-295`), with no list of drafts.

### 10. A campaign is deleted

- **Today:** only a campaign that was never activated and has no message rows can be deleted, so it has no campaign drafts. Its test-send drafts stay in Gmail, and the delete plan lists them (`netkeeper/services/campaigns.py:1140-1147`; `netkeeper/web/api/campaigns.py:255-266`).
- **Trash?** Yes, a candidate, for the same reason as case 11.
- **By hand:** yes. The delete dialog lists each test draft left behind, with its step, address, and date (`frontend/src/features/campaigns/campaign-detail-page.tsx:416-430`).

### 11. Review and arming test drafts

- **Today:** a test send on a mailbox armed for drafts only makes a test draft addressed to the mailbox itself (`netkeeper/services/campaign_review.py:63`). The Message-ID check searches for the newest of them and never touches them (`netkeeper/services/mailboxes.py:339-356`). They stay in Drafts until you delete them.
- **Trash?** Yes, the safest candidate. netkeeper made them, they go only to you, and no campaign message depends on them. Trash one only after the mailbox is verified, since an unverified mailbox needs one to verify (#304).
- **By hand:** yes. Leftover drafts lists each test draft, and says to keep it until the mailbox's Message-ID check passes.

### 12. A mailbox is disconnected or its token expires

- **Today:** disconnecting forgets the token, disables the mailbox, and disarms it (`netkeeper/services/mailboxes.py:256-264`). A disarmed mailbox's drafts aren't polled (`netkeeper/services/campaign_sender.py:95-97`), so they stay `drafted` until the mailbox is connected and armed again. An expired token pauses email steps the same way until you sign in again.
- **Trash?** No. Without a token netkeeper can't reach Gmail, and a disconnect is often you withdrawing access, which is the wrong moment to change your mailbox.
- **By hand:** no. The disconnect dialog doesn't mention drafts (`frontend/src/features/settings/gmail-section.tsx:293-295`).

### 13. Import rollback

- **Today:** a rollback is refused outright when any contact the run created has a campaign message of any status (`netkeeper/crm/import_runs.py:58-61`, `:1444-1449`). No rollback can strand a draft.
- **Trash?** Never needed.
- **By hand:** not applicable.

### 14. A contact is archived

- **Today:** archiving sets `archived_at` only (`netkeeper/crm/contacts.py:487-494`). The guards skip the contact at the next step fire (`netkeeper/services/campaign_guards.py:253-254`). A waiting draft stays `drafted` and is still tracked. Rejecting a contact read off a card (`contacts.py:524-541`) can't leave a draft: a contact waiting for review is never enrolled (`campaign_guards.py`, `not_waiting_for_review`).
- **Trash?** No. You undo an archive with one click in netkeeper; a trash is a step in Gmail you'd have to undo yourself.
- **By hand:** no.

### 15. Schedule send (#278)

- **Today:** a draft you schedule leaves Drafts before anything is sent. The drafts poll finds it in Scheduled and keeps it `drafted` until it goes out (`netkeeper/services/campaign_sender.py:56-68`).
- **Trash?** No. Missing from Drafts isn't deleted.
- **By hand:** not applicable.

### 16. A draft edited into a new thread (#280)

- **Today:** a draft you edit into a new thread and send reads as `discarded`, and its enrollment ends `removed` (`netkeeper/services/campaign_sender.py:71-74`). This is a known limitation.
- **Trash?** No. netkeeper's reading is wrong here, so it must never act on it.
- **By hand:** not applicable.

### 17. A step adopts a new template version

- **Today:** a message that exists keeps its text: a waiting draft isn't rendered again (`netkeeper/services/template_adoption.py:9-19`).
- **Trash?** No. Trashing to make a fresh draft would throw away edits you made.
- **By hand:** partly. The adoption preview counts the messages that keep their text (`campaign-detail-page.tsx:808-810`).

### 18. A LinkedIn step changes mode (#536)

- **Today:** on `main`, a step's mode is fixed when the campaign is built, and a mailbox taken back to drafts only changes how the next steps fire. PR #539 (open) proposes a switch for LinkedIn steps only, and refuses an email step.
- **Trash?** Not applicable: no Gmail draft is involved.
- **By hand:** not applicable.

### 19. Reconcile gives up on a leftover

- **Today:** a message left `scheduled` by a crash or an unknown outcome is searched for by its Message-ID. After enough empty searches, it's `discarded` when nothing would send it, or `failed` for a person (`netkeeper/services/campaign_engine.py:2448-2509`). If the write was a `drafts.create` that did reach Gmail, and Gmail's search never found it, the draft stays in Drafts with no `gmail_draft_id` recorded.
- **Trash?** Yes, a candidate, but only once the draft is found by its Message-ID and matched as ADR 0009 requires. A draft netkeeper never recorded can't meet the draft id rule, so today the switch would leave it.
- **By hand:** no. The enrollment table shows a `failed` message's error, not a draft that may remain.

## The cleanup list

The Gmail page's Leftover drafts card lists the drafts netkeeper no longer wants (#543; `netkeeper/services/leftover_drafts.py`). The P3-11 list this catalogue's issue assumed was never built. Each row has an **Open in Gmail** link, which searches the mailbox's account for the draft's Message-ID. The list covers:

- `discarded` messages that still have a `gmail_draft_id` (case 1).
- `drafted` messages whose enrollment isn't `pending`, `active`, or `paused` (cases 2 to 6).
- `drafted` messages on a live enrollment whose contact is do not contact or has an address on the do-not-send list (case 7).
- Test sends with a `gmail_draft_id` (case 11).

A draft for a contact who opted out, bounced, is do not contact, or is on the do-not-send list is flagged and listed first, and also puts a dot on the Gmail nav item. A draft left after a reply is listed last. The merge preview says that discarded drafts stay in Gmail and appear in this list. The list only reads: it never calls Gmail, and it deletes nothing.
