# 0008. Auto-send clicks Send, only from the scheduler, and stops at the first doubt

Date: 2026-10-07

## Status

Accepted (#384, merged in PR #458 on 2026-10-07).

Amends [ADR 0007](0007-prefill-inputs.md), which says no code path clicks Send. This ADR permits one Send click, for one mode. Everything else in ADR 0006 and ADR 0007 still holds.

## Context

[ADR 0004](0004-manual-linkedin-sends-by-default.md) defines an opt-in `auto_send` step mode behind `[campaigns] linkedin_auto_send`, with its own budget. The flag, the mode, and the `li_messages_auto` budget exist, but nothing sends. At CP8 the maintainer decided to build it.

Auto-send differs from a prefill in two ways that drive every rule below:

- Nobody is watching. A prefill types and stops; a person reads the composer and clicks Send. An auto-send runs on a schedule, and a wrong recipient or a wrong body goes out.
- A send can't be taken back, and automated messaging is what LinkedIn restricts accounts for fastest. A restricted account ends the whole workflow.

## Decision

netkeeper may give a LinkedIn page three more inputs, all in `BrowserRun` (`netkeeper/linkedin/browser.py`) and all for an auto-send alone: one click on Send (`click_send`), then one click on the sent bubble's close control (`close_sent_bubble`), then closing the run's own tab (`close_sent_tab`).

The rules that make this acceptable:

1. **Only the scheduler triggers it.** `netkeeper serve` schedules an `auto_send` job only when the flag is on at start; changing the flag takes a restart. A person who prefills an `auto_send` step by hand gets a prefill, which never clicks Send.
2. **Every gate is checked twice: before typing, and again after a dwell, right before the click.** The gates are the flag, arming, no pause, no hold, the claim still wanted (campaign and enrollment active, no reply, step still `auto_send`, contact not do-not-contact), active hours, the session not flagged, heat under its skip threshold, and no cancel. Anything that changed while typing stops the click and leaves the typed message for the person.
3. **The Send control is verified as strictly as every key.** Exactly one button named **Send**, inside the verified composer's form, with the composer's text equal to what was typed. One click, never retried, never twice.
4. **A landed click isn't proof.** The run reads the page's own `createMessage` response (ADR 0006: observed, never sent). Only a 200 whose body text matches the typed text counts as sent; only a proven send closes the bubble and the tab.
5. **Any other ending holds auto-send until a person resumes it.** An auto-send that clicked Message and didn't close its own tab (a refusal, a partial type, an unproven send, a bubble or tab that stayed open, a crash mid-run) writes a hold, and so do three refusals before the click that the next run would meet again (a bubble already open or unreadable, no clear Message control). While held, the scheduler claims nothing, the posture row and the LinkedIn queue say why, and you clear it by closing the bubble in Chrome and confirming (**I fixed it in Chrome, resume auto-send**, or `netkeeper linkedin auto-send-resume`). netkeeper never lifts a hold by itself.
6. **Separate budget, same heat rule.** An auto-send spends `li_messages_auto` and never `li_prefills`. Heat pauses auto-send outright at the skip threshold; it never shrinks the budget. A run that typed nothing and attempted no Send click refunds its `li_messages_auto` unit (never the profile visit); once a key or the Send click was attempted, nothing is refunded.
7. **Pinned by tests.** `tests/test_browser_safety.py` lists the two new clicks, pins `close_sent_tab` to one caller under `bubble_closed`, pins that `click_send` is reachable only from `PagePrefill.prefill` with a `SendPermit` (built in one place, after the budget consume), and allows the literal `"Send"` in exactly one locator. Each gate has a mutation test.

The gates in the order the code checks them, the proof-of-send and close rules, the hold's exact triggers, the refunds, and the pins are in [How netkeeper drives a LinkedIn page](../linkedin-browser-inputs.md#auto-send). The numbers are in [architecture.md](../architecture.md) sections 9.6 and 11.6 and `budgets.py`.

## Consequences

- With the flag off, nothing schedules, claims, types, or clicks. ADR 0004's default stands.
- With the flag on, netkeeper sends LinkedIn messages with nobody watching. The account risk is real and the posture page says so while the flag is on.
- An open bubble stops the whole queue, by design. Working past it spends clicks and visits, and a second attempt could send the body twice. The person clears it.
- An auto-send brings its tab forward at the start, as a prefill does. If you type in the netkeeper Chrome window while one runs, your keys can land in the composer. Keep LinkedIn's **Press Enter to Send** off in that profile, and stay out of that window while auto-send is on.
- If LinkedIn changes the Send control, the close control, or the `createMessage` response, the click refuses or the send goes unproven, and auto-send holds. That's the safe direction.
- A future contributor keeps these true: the only clicks are Contact info, Message, Send, and the sent bubble's close control, each once; Send is clicked only by `click_send`, only for a scheduled run with the flag on, after the dwell and the recheck; nothing clicks Send twice; nothing closes a bubble or a tab before a proven send; and no hold is lifted without a person's confirmation.

## Amendments

- 2026-10-10 (#499): the sent bubble's close control is matched by the `Close your conversation with ` prefix plus the header name contained in it, not by an exact name, because the live button's name carries extra text. Every other check and refusal stands.
- 2026-10-14 (#516): the CP8c captures show the close control's extra text is the account owner's own name: `Close your conversation with <OWNER> and <NAME>`, in either order. Past the prefix, the name must now be exactly the header name, alone or with one other non-empty name across a single ` and `. The owner's name isn't compared, since netkeeper doesn't reliably know your LinkedIn name at the click. Every #499 check and refusal stands. A second bubble or composer that draws after the pre-click check, and so makes the check after the Message click refuse, holds auto-send. Item 5 already covered it: the Message click was attempted and the tab stayed open. A test now pins it.
