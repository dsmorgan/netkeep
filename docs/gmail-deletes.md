# Gmail drafts netkeeper leaves behind

netkeeper deletes nothing in Gmail ([ADR 0003](adr/0003-gmail-api-over-smtp.md)). Its Gmail interface has no delete or trash call (`netkeeper/campaigns/gmail.py:247-310`), and its one label write, `modify_labels`, is called only to add the campaign's label (`netkeeper/services/campaign_sender.py:720`). This page lists every case where netkeeper stops caring about something it put in Gmail, so a future switch that trashes drafts can be designed once (#279). The rules that switch must follow are in [ADR 0009](adr/0009-gmail-deletes.md).

Each case says:

- **Today:** what netkeeper does now, with the code that does it.
- **Trash?** Whether trashing the draft would ever be right.
- **By hand:** whether you can see the leftover draft in netkeeper's UI and clean it up yourself.

Line numbers are as of `acfe01b5` on `main`.

## Summary

| Case | Today | Trash ever right? | Shown for cleanup in the UI? |
|---|---|---|---|
| 1. A merge discards a waiting draft | Message marked `discarded`; draft stays | Yes, a candidate | No |
| 2. A detected reply ends the enrollment | Enrollment `replied`; draft stays `drafted` | No | No |
| 3. A detected opt-out ends the enrollment | Enrollment `opted_out`; draft stays `drafted` | Yes, the strongest candidate | No |
| 4. A bounce ends the enrollment | Enrollment `bounced`; draft stays `drafted` | Yes, a candidate | No |
| 5. You remove an enrollment | Enrollment `removed`; draft stays `drafted` | Maybe, only if you ask | No |
| 6. You record an outside response (#512) | Ends like case 2 or 3 | As case 2 or 3 | No |
| 7. You mark a contact do not contact, or add an address to do-not-send | Nothing changes until the next step fires | Yes, as case 3 | No |
| 8. A campaign is paused or ended | Campaign status only; drafts stay | No | Partly: the end dialog warns |
| 9. A campaign is archived | Only an ended campaign; drafts stay | No | No |
| 10. A campaign is deleted | Only a never-activated one; its test drafts stay | Yes, a candidate | Yes, listed |
| 11. Review and arming test drafts | Kept; searched for, never touched | Yes, the safest candidate | Only on campaign delete |
| 12. A mailbox is disconnected or its token expires | Token forgotten; drafts stay `drafted` | No | No |
| 13. Import rollback | Refused if a created contact has any message | Never needed | Not applicable |
| 14. A contact is archived or rejected | Contact archived; draft stays `drafted` | No | No |
| 15. Schedule send (#278) | Tracked in Scheduled; stays `drafted` | No | Not applicable |
| 16. A draft edited into a new thread (#280) | Reads as `discarded`; enrollment `removed` | No | Not applicable |
| 17. A step adopts a new template version | Waiting draft keeps its text | No | Partly: adoption preview |
| 18. A LinkedIn step changes mode (#536) | No Gmail draft involved | Not applicable | Not applicable |

No list of leftover drafts exists in the UI. See [The cleanup list](#the-cleanup-list).

## The cases

### 1. A merge discards a waiting draft

- **Today:** when two merged contacts both have an enrollment in one campaign, the outranked enrollment's unsent messages are marked `discarded` (`netkeeper/crm/identity.py:1520-1532`; the statuses are at `identity.py:1388-1395`). Only the status changes, so the message keeps its `gmail_draft_id`, and the Gmail draft stays in Drafts. The drafts poll tracks only `drafted` messages (`netkeeper/services/campaign_engine.py:2301-2308`), so netkeeper stops watching that draft, and if you send it anyway, the send may go unrecorded.
- **Trash?** Yes, it's a candidate. netkeeper made the draft, nothing will send it, and sending it would repeat a step the surviving enrollment holds. Trash, not delete, because you may have edited it.
- **By hand:** no. The merge preview says how many unsent messages are discarded (`frontend/src/features/contacts/merge-text.ts:115-118`), but not that their Gmail drafts stay or which ones. Afterward, the Gmail page's recent card can show a `discarded` pill (`frontend/src/features/gmail/recent-card.tsx:12-20`), but only among the newest 20 email messages (`netkeeper/services/gmail_activity.py:55`), and it doesn't say a draft remains.

### 2. A detected reply ends the enrollment

- **Today:** the reply poll ends the enrollment `replied` (`netkeeper/services/campaign_replies.py:640-677`), and ending an enrollment changes no message (`netkeeper/services/campaign_engine.py:872-891`). The waiting draft stays `drafted`, and the drafts poll still records it `sent` if you send it.
- **Trash?** No. You may want to edit the draft and send it as your answer.
- **By hand:** no. The campaign's enrollment table shows the status, the exit reason, and a link to the inbox (`frontend/src/features/campaigns/campaign-detail-page.tsx:1210-1235`), but not that a draft waits.

### 3. A detected opt-out ends the enrollment

- **Today:** an unsubscribe reply sets the contact `do_not_contact`, puts the address on the do-not-send list, and ends the enrollment `opted_out` (`netkeeper/services/campaign_replies.py:55-57`, `:653-673`). The draft stays `drafted`.
- **Trash?** Yes, the strongest candidate. Sending that draft by hand would ignore the opt-out, and nothing in netkeeper can stop a send you make in Gmail.
- **By hand:** no. Nothing tells you a draft waits for someone who opted out.

### 4. A bounce ends the enrollment

- **Today:** the enrollment ends `bounced` (`netkeeper/services/campaign_replies.py:710`) and the address goes on the do-not-send list. The draft stays `drafted`.
- **Trash?** Yes, a candidate. The draft is addressed to an address that bounced.
- **By hand:** no, as in case 2.

### 5. You remove an enrollment

- **Today:** `remove_enrollment` ends it `removed` and changes no message (`netkeeper/services/campaign_engine.py:859-869`). The draft stays `drafted`.
- **Trash?** Maybe, but only as a choice you make in the removal itself. You removed the contact from the sequence, which doesn't say what you want done with a draft you may have edited.
- **By hand:** no.

### 6. You record an outside response (#512)

- **Today:** a response you record (a call, a text, in person) ends every live enrollment of the contact through the same path as a detected reply, or as an opt-out when you say so (`netkeeper/services/outside_responses.py:33-40`, `:272-283`). Drafts stay `drafted`.
- **Trash?** As case 2 for a response, and as case 3 for an opt-out.
- **By hand:** no. The record-response dialog doesn't mention waiting drafts.

### 7. You mark a contact do not contact, or add an address to do-not-send

- **Today:** nothing changes at once. The guards check at the next step fire (`netkeeper/crm/do_not_send.py:1-6`; `netkeeper/services/campaign_guards.py:262`, `:300`), and a do-not-contact contact's enrollment then ends `opted_out` (`netkeeper/services/campaign_engine.py:217-220`). A draft step's next step is scheduled only after the draft is seen sent, so while the draft waits, no step fires and nothing checks.
- **Trash?** Yes, as case 3.
- **By hand:** no.

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
- **By hand:** only when you delete the campaign (case 10).

### 12. A mailbox is disconnected or its token expires

- **Today:** disconnecting forgets the token, disables the mailbox, and disarms it (`netkeeper/services/mailboxes.py:256-264`). A disarmed mailbox's drafts aren't polled (`netkeeper/services/campaign_sender.py:95-97`), so they stay `drafted` until the mailbox is connected and armed again. An expired token pauses email steps the same way until you sign in again.
- **Trash?** No. Without a token netkeeper can't reach Gmail, and a disconnect is often you withdrawing access, which is the wrong moment to change your mailbox.
- **By hand:** no. The disconnect dialog doesn't mention drafts (`frontend/src/features/settings/gmail-section.tsx:293-295`).

### 13. Import rollback

- **Today:** a rollback is refused outright when any contact the run created has a campaign message of any status (`netkeeper/crm/import_runs.py:58-61`, `:1444-1449`). No rollback can strand a draft.
- **Trash?** Never needed.
- **By hand:** not applicable.

### 14. A contact is archived or rejected

- **Today:** archiving or rejecting sets `archived_at` only (`netkeeper/crm/contacts.py:487-494`, `:524-541`). The guards skip the contact at the next step fire (`netkeeper/services/campaign_guards.py:253-254`). A waiting draft stays `drafted` and is still tracked.
- **Trash?** No. Archiving is undone with one click; a trash isn't.
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

- **Today:** the switch in PR #539 applies only to LinkedIn steps and refuses an email step. An email step's mode is fixed when the campaign is built, and a mailbox taken back to drafts only changes how the next steps fire.
- **Trash?** Not applicable: no Gmail draft is involved.
- **By hand:** not applicable.

## The cleanup list

The issue expected P3-11 to list drafts whose message is `discarded` or whose enrollment has ended. P3-11 shipped without one ([implementation guide](implementation-guide.md), P3-11), and no such list exists. Until a switch exists, you can't find these drafts in netkeeper. A follow-up issue should add the list. The data is there:

- `discarded` messages that still have a `gmail_draft_id` (case 1).
- `drafted` messages whose enrollment isn't `pending`, `active`, or `paused` (cases 2 to 7), opt-outs and bounces first.
- Test sends with a `gmail_draft_id` (case 11).

Each row needs an **Open in Gmail** link, and the merge preview needs to say that discarded drafts stay in Gmail.
