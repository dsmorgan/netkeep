# 0007. The prefill's inputs: one Message click, typing into one verified composer, never Enter

Date: 2026-10-03

## Status

Accepted (2026-10-06, with the [decisions recorded at acceptance](#decisions-recorded-at-acceptance)).

The mechanics this ADR once spelled out (locator rules, CDP methods, the order of the checks, refusal categories, and the test pins) moved to [How netkeeper drives a LinkedIn page](../linkedin-browser-inputs.md#the-prefill) on 2026-10-11. The decision is unchanged.

## Context

[ADR 0004](0004-manual-linkedin-sends-by-default.md) makes `prefill` the default LinkedIn step mode: netkeeper types the message into the conversation in your Chrome and stops, and you click Send. It doesn't say how that typing is made safe. [ADR 0006](0006-observe-dont-request.md) allows three inputs to a LinkedIn page: navigation, the scroll replay, and one **Contact info** click per profile visit. A prefill can't be built under those rules, and it shouldn't be built by loosening them case by case in code review.

Three risks shape the exceptions:

- **A sent message can't be taken back.** A key that sends, or a click on Send, turns a prefill into the automated message ADR 0004 exists to prevent. Whether Enter sends depends on LinkedIn's **Press Enter to Send** setting, which netkeeper doesn't control.
- **Typing into the wrong place is worse than not typing.** A composer for another person, one that already holds a draft, a second bubble left open, or keys that arrive after focus moved would each put the body where you didn't intend.
- **The tab is yours after the prefill.** You have to find the typed message and click Send. A run that reused or closed that tab would destroy the draft.

The 2026-10-05 messaging capture (#374, [linkedin-messaging-shapes.md](../linkedin-messaging-shapes.md)) settled the facts the checks rest on, among them that Shift+Enter inserts a newline whatever the setting, while Enter and Command+Enter can send.

## Decision

netkeeper may give a LinkedIn page three new inputs, all in `BrowserRun` (`netkeeper/linkedin/browser.py`): one click on **Message**, typing into one verified composer, and at most one `Locator.focus()` on that composer. It also gets one new way to end a run: handing its tab over instead of closing it. Everything else in ADR 0006 stays: no script in the page, no request of netkeeper's own, no interception.

The rules that make this acceptable:

1. **A person triggers each prefill and watches.** You click **Prefill**; nothing prefills on a schedule. A run that can't take the browser lock at once, or whose claim is over 60 seconds old, refuses before it spends anything. The tab comes to the front once, at the start, and nothing later changes which window is in front, because a change while you type elsewhere could misdirect your keys.
2. **Nothing is spent before the body is known to be typeable.** The typing plan is built first; a body it refuses costs no budget and no navigation. Before the first key, every refusal is `not_typed` and costs at most a budget unit. After it, a stop is `partially_typed` or `unknown`, never retried: you clear the composer.
3. **The Message click is bound to the contact, not to a position.** Every **Message** link on the page must carry a compose `href` for this contact's URN, or nothing is clicked. The click goes through a locator bound to that `href`, picks a control that is on screen and uncovered (read from the page's geometry over a read-only CDP session, never by script or hover), and is made once. A bubble already open before the click refuses it.
4. **Typing goes into exactly one verified, empty composer whose recipient is this contact.** The recipient is proved three ways, and all three must agree: the Message links, the compose option the page loaded, and the bubble itself. Any other composer on the page, open or minimized, refuses: netkeeper never closes a bubble, because closing one deletes its draft.
5. **Every check runs again before every key, with focus last.** Playwright sends keys to whatever has focus, and a Shift+Enter on a focused Send button would send. So each step is delay, checks, key, with nothing awaited between the focus read and the key. A failed check stops typing.
6. **Shift+Enter is the only key pressed.** Enter, Command+Enter, and every modifier are never pressed. A space is inserted as text, never typed, because the Space key activates a focused button. No locator under `netkeeper/linkedin/` names Send or Submit (ADR 0008 later allows one, `"Send"`, inside `BrowserRun._send_control`).
7. **One focus call, at most.** Where focus lands after the click is unknown. netkeeper may call `focus()` on the verified composer once, after every other check passed, and types nothing if the composer still doesn't hold focus. With no focus input at all, every prefill would refuse until an amendment.
8. **After the click, the tab isn't netkeeper's.** The run never navigates or reattaches (a reopened tab would have no verified composer), and at the end it hands the tab over instead of closing it, whatever the outcome. No later run reuses, navigates, or closes it. `prefilled_at` is taken before the first key, so the inbox poll can match your send.

`tests/test_browser_safety.py` pins all of this: the three inputs at one site each, the single `press` literal `"Shift+Enter"`, no Send locator, the `href`-bound click, and that nothing after the click navigates, scrolls, or observes.

## Consequences

- netkeeper can type a message into LinkedIn, and no key or click of netkeeper's is meant to send one. What you see in the handed-over tab is the rendered body, in the right conversation, waiting for your click. Multi-line templates work.
- The recipient probably sees "typing…" while the prefill types, for up to 300 seconds, even if you never send. The maintainer accepted this on 2026-10-05; #430 tracks a possible toggle, which would need its own amendment.
- Each prefill spends one `li_prefills` unit and one `profile_visits` unit before it navigates. A contact without a public id can't be prefilled.
- You close earlier bubbles before the next prefill, and handed-over tabs accumulate until you close them. That's the price of never losing a draft.
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
- 2026-10-11: design detail moved to [linkedin-browser-inputs.md](../linkedin-browser-inputs.md). Decision unchanged. The invariant "no code path presses Enter or clicks Send" now reads "no code path in a prefill", with a pointer to ADR 0008, which made that exception on 2026-10-07.
