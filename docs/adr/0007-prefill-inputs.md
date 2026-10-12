# 0007. The prefill's inputs: one Message click, typing into one verified composer, never Enter

Date: 2026-10-03

## Status

Accepted (2026-10-06, with the [decisions recorded at acceptance](#decisions-recorded-at-acceptance)).

The mechanics this ADR once spelled out (locator rules, CDP methods, the order of the checks, refusal categories, and the test pins) moved to [How netkeeper drives a LinkedIn page](../linkedin-browser-inputs.md#the-prefill) on 2026-10-11. The decision is unchanged.

## Context

[ADR 0004](0004-manual-linkedin-sends-by-default.md) makes `prefill` the default LinkedIn step mode: netkeeper types the message into the conversation in your Chrome and stops, and you click Send. It doesn't say how that typing is made safe.

[ADR 0006](0006-observe-dont-request.md) allows netkeeper three inputs to a LinkedIn page: navigation, the scroll replay, and one **Contact info** click per profile visit. `tests/test_browser_safety.py` refuses every other click, key, focus, or synthetic event. A prefill can't be built under those rules, and it shouldn't be built by loosening them case by case in code review.

Three risks shape the exceptions:

- **A sent message can't be taken back.** A key that sends, or a click on Send, turns a prefill into the automated message ADR 0004 exists to prevent. LinkedIn's **Press Enter to Send** setting decides whether Enter sends, and netkeeper doesn't control it.
- **Typing into the wrong place is worse than not typing.** A composer for another person, one that already holds a draft, a second bubble left open from an earlier visit, or keys that keep arriving after focus moved would each put the body where you didn't intend.
- **The tab is yours after the prefill.** You have to find the typed message and click Send. A run that reused or closed that tab would destroy the draft.

The 2026-10-05 messaging capture (#374, [linkedin-messaging-shapes.md](../linkedin-messaging-shapes.md)) settled the facts the checks rest on: the profile renders three identical **Message** links bound to the contact's URN; the click opens a bubble on the profile page and loads a compose option that names the recipient; the composer's label doesn't name the recipient; and Shift+Enter inserts a newline whatever the setting, while Enter and Command+Enter can send.

## Decision

netkeeper may give a LinkedIn page three new inputs, all in `BrowserRun` (`netkeeper/linkedin/browser.py`): one click on **Message**, typing into one verified composer, and at most one `Locator.focus()` on that composer. It also gets one new way to end a run: handing its tab over instead of closing it. Everything else in ADR 0006 stays: no script in the page, no request of netkeeper's own, no interception.

The rules that make this acceptable:

1. **A person triggers each prefill and watches.** Due steps wait in a queue; you click **Prefill**. Nothing prefills on a schedule. A run that can't take the browser lock at once, or whose claim is older than 60 seconds, refuses before it spends anything, so a prefill never starts after you've stopped watching. A refused or interrupted prefill is never retried or resumed by itself.
2. **The typing plan is built before anything is spent.** A body the plan refuses (too long, an unsupported character, a line break other than LF) costs no budget and no navigation.
3. **The tab comes to the front once, at the start.** Nothing later changes which tab or window is in front, because a change while you type elsewhere could send your keys somewhere you didn't intend.
4. **The Message click is bound to the contact, not to a position.** Every **Message** link on the page must carry a compose `href` for this contact's URN, or nothing is clicked. The click goes through a locator bound to that `href`, picks a control that is on screen and uncovered (read from the page's geometry over a read-only CDP session, never by script or hover), and is made once, never retried. A bubble already open before the click refuses it.
5. **After the click, the run never navigates or reattaches.** A reopened tab would be a page with no verified composer.
6. **Typing goes into exactly one verified, empty composer whose recipient is this contact.** The recipient is proved three ways, and all three must agree: the Message links, the compose option the page loaded, and the bubble itself (the header link's profile id for an existing conversation; the chip and card for a new one). The bubble alone never authorizes typing, and neither does the compose answer alone. Any other composer on the page, open or minimized, refuses: netkeeper never closes a bubble, because closing one deletes its draft.
7. **Every check runs again before every key, with focus last.** Playwright sends keys to whatever has focus. If focus moved mid-type, a Shift+Enter could land on Send. So each step is delay, checks, key, with nothing awaited between the focus read and the key, and any failed check stops typing as `partially_typed`.
8. **Shift+Enter is the only key pressed.** The capture shows it never sends, whatever the setting. Enter, Command+Enter, and every modifier are never pressed. A space is inserted as text, never typed, because the Space key activates a focused button. No locator under `netkeeper/linkedin/` names Send or Submit.
9. **One focus call, at most.** Where focus lands after the click is unknown. netkeeper may call `focus()` on the verified composer once, only after every other check passed, and types nothing if the composer still doesn't hold focus. The alternative, no focus input, would have every prefill refuse until an amendment.
10. **The tab is handed over, never closed.** Once the Message click was attempted, the tab isn't netkeeper's: no later run reuses, navigates, or closes it, whatever the outcome. `prefilled_at` is taken before the first key, so the inbox poll can match your send.
11. **The point of no return is the first key.** Before it, every refusal is `not_typed` and costs at most a budget unit. After it, a stop is `partially_typed` or `unknown`, and you clear the composer.

`tests/test_browser_safety.py` pins all of this: the three inputs at their one site each, the single `press` literal `"Shift+Enter"`, the absence of any Send locator, the `href`-bound click, and that nothing after the click navigates, scrolls, or observes.

## Consequences

- netkeeper can type a message into LinkedIn, and no key or click of netkeeper's is meant to send one. What you see in the handed-over tab is the rendered body, in the right conversation, waiting for your click.
- Multi-line LinkedIn templates work, typed with Shift+Enter.
- The recipient probably sees "typing…" while the prefill types, for up to 300 seconds, even if you never send. The maintainer accepted this on 2026-10-05; #430 tracks a possible toggle, which would need its own amendment.
- Each prefill spends one `li_prefills` unit and one `profile_visits` unit before it navigates.
- Any other bubble on the page stops the prefill. You close earlier bubbles, including the last prefill's, before the next one. Handed-over tabs accumulate until you close them; that's the price of never losing a draft.
- A contact without a public id can't be prefilled.
- **The remaining risk is a send.** Focus is checked right before each key, not atomically with it. If focus moves to Send between the check and a Shift+Enter, the Shift+Enter could send what's typed so far. This window is why you watch and leave Chrome alone.
- If LinkedIn changes the Message control, the compose option, the composer, or how the bubble shows its recipient, the prefill refuses and types nothing. If LinkedIn changes what Shift+Enter does, nothing detects it at run time: the capture is the evidence, and a later capture that contradicts it means newlines are refused until a new amendment.
- A future contributor keeps these true: the only clicks are Contact info and Message, each once, each bound to the profile it's on; the only typing is aimed at one verified composer, with every check repeated right before every key; the only key pressed is Shift+Enter; no code path in a prefill presses Enter or clicks Send ([ADR 0008](0008-auto-send.md) carves out the one Send click, for a scheduled auto-send only); and a handed-over tab is never touched again.

## Decisions recorded at acceptance

The maintainer accepted these on 2026-10-06:

1. **Where the tab comes to the front.** Once, at the run's start, before the Message click; `hand_over()` changes no focus. That adjusts the "brought to the front" wording of 2026-10-03.
2. **The claim lapse.** 60 seconds after `Message.scheduled_at`.
3. **Other open bubbles.** The prefill refuses a page that shows any composer besides the one the click opened, hidden ones included.
4. **The Message click rule.** Every link named Message must name the contact's URN and bare id, and the click goes through a locator bound to that `href`. It replaces the draft's "exactly one, in the top card" rule.
5. **Focusing the composer.** One `Locator.focus()`, in `_focus_seam`, after every other check (option B).

## Amendments

Each entry's mechanics are in [How netkeeper drives a LinkedIn page](../linkedin-browser-inputs.md#the-prefill).

- 2026-10-06 (#444): the click chooses, among the verified links, one that is on screen with nothing over its click point, read from the page's geometry over a read-only CDP session; a bubble already on the page refuses before the click. CP8's clicks had raised on a sticky header's off-screen copy.
- 2026-10-07 (#470): when the brief pre-click scroll leaves the top card's control under the sticky header, the prefill scrolls back up once before the click. Also fixes the hit test's coordinates on a scrolled page.
- 2026-10-08 (#473): `netkeeper linkedin message-check <contact id>` runs the prefill's reads up to the click and reports why it would refuse, with no input and no page text.
- 2026-10-10 (#481): the new-message bubble's card may link to the contact more than once and by member id as well as slug; any link to anyone else still refuses. The profile page no longer has an `h1`, so the chip-name check is skipped and the card's link binds the recipient.
- 2026-10-10 (#490): a `::first-letter` pseudo-element at the click point is attributed to its host through one more read-only CDP method, so it no longer reads as covering the link.
- 2026-10-10 (#495): `message-check --bubble` reports the shape of a sent bubble's close control, for ADR 0008's close click.
- 2026-10-11: design detail moved to [linkedin-browser-inputs.md](../linkedin-browser-inputs.md). Decision unchanged.
