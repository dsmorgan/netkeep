# 0014. A menu-bar status app in the locally built netkeep.app

Date: 2026-10-22

## Status

Accepted (#755, 2026-10-22), by the maintainer. This ADR amends rules 6 and 9 of [ADR 0011](0011-installation-and-updates.md).

## Context

ADR 0011 left a menu-bar icon to a new ADR. On 2026-10-22 the maintainer agreed to build one without the Apple Developer Program (option T1). It builds on the launcher bundle (#754) and on the three status chips (#751).

The facts the decision rests on:

- **PyObjC installs from wheels on both architectures.** pyobjc-core and pyobjc-framework-Cocoa 12.2.2 ship a `cp312` universal2 wheel.
- **rumps is barely maintained.** Its last release, 0.4.0, is from October 2022.
- **Any Python process can own an `NSStatusItem`.** An accessory activation policy keeps the process out of the Dock; `LSUIElement` does the same from a bundle.
- **Notifications need a real bundle.** `UNUserNotificationCenter` throws in an unbundled process. `osascript` notifications appear as Script Editor's, and only after you allow Script Editor's notifications.
- **macOS 26 lets you hide any third-party app's menu-bar item.**
- **Gatekeeper's first-launch check applies to quarantined files,** and a bundle written on the Mac isn't quarantined.

## Options

- **a. PyObjC, from a helper inside netkeep.app (chosen):** it needs no account and no third-party install.
- **b. rumps:** a thin wrapper over the same calls, and unmaintained.
- **c. A Swift or signed app:** it needs the Apple Developer Program.
- **d. SwiftBar or xbar:** a third-party app to install and trust.
- **e. No menu bar:** the chips show only while the window is open.

## Decision

Option a. `netkeep setup` adds a helper bundle inside `netkeep.app`, with its own bundle id and `LSUIElement`. The helper runs the installed `netkeep menubar`, a PyObjC `NSStatusItem` with an accessory activation policy. The icon is tinted by the worst of the three chips. The menu shows the chip lines, **Open netkeep**, **Check replies now**, **Pause** or **Resume LinkedIn scheduled runs**, **Update to X.Y.Z** (only when the daily check found one), and **Quit netkeep**. There are no notifications in 1.0. [The menu-bar app](../design/menu-bar.md) holds the mechanics.

### Rules

1. **The server rolls up its own parts, and each client adds its connection state.** The UI and the menu bar read one read-only endpoint. Each adds what only it can know (reachable, live updates) by the same worst-wins rule. *Reason:* a second copy of #751's rules would drift.
2. **It's a local client.** It sends the Keychain key as a bearer token (ADR 0013) to its own data directory's `serve`, on loopback only.
3. **A Keychain refusal or a `401` is shown, never retried in a loop.** The icon shows "off", the menu says why, and the key is read again only when you open the menu. A `401` means `serve` runs on a key held in memory (ADR 0013, rule 8). **Open netkeep** and **Quit netkeep** still work. *Reason:* no dead end and no stacked prompts.
4. **It acts only on your click, from a fixed list:** `netkeep open`, the replies check, LinkedIn schedule pause and unpause, `netkeep update`, and the login item's stop. It never arms anything, starts a run, sends, changes a setting, or launches Chrome except through `netkeep open`. Resume starts nothing at once (#324). A test pins the list. *Reason:* a background process does only what you ask.
5. **Update and Quit ask first.** The confirmation names any LinkedIn run or campaign batch in progress, because `update` stops `serve` mid-run.
6. **The menu bar outlives an update without running replaced code.** It loads everything at startup. While `serve` is down, it reads progress from the update's state file in the data directory. `update --finish` restarts it last. *Reason:* `uv tool install` replaces the code it runs on.
7. **Quit ends both processes, and Open netkeep or your next login brings both back.** Quit stops `serve` with the login item's existing stop, then the menu bar exits. The launcher re-adds any job that launchd doesn't have, then kickstarts it. *Reason:* the stop removes the job from launchd, so a kickstart alone would fail.
8. **One Run-at-login switch for two launchd jobs, plus a Show menu-bar icon preference.** Run at login, `setup`, and `update` manage both jobs from one implementation. They add the menu-bar job only while the preference is on, and they remove it when the preference is off. The menu-bar job's label follows the login item's per-data-directory label, and the job never takes the serve lock. *Reason:* `update` stops `serve` while the menu bar shows progress, and no two jobs race for the lock.
9. **Hiding the icon is your choice.** netkeep doesn't fight macOS 26's setting. *Reason:* netkeep works fully without it, and the preference stops the process.
10. **No notifications in 1.0.** The icon's tint is the signal. *Reason:* a notification from "Script Editor" confuses more than it helps.
11. **PyObjC is a macOS-only dependency** in the release's constraints file, and the Intel CI job imports it. *Reason:* Linux and the container don't need it.

### Amendments to ADR 0011 (2026-10-22)

- **Rule 6 becomes:** "`netkeep setup` and the #600 Settings switch are one implementation of one Run-at-login setting. It manages the `serve` job and, under ADR 0014, a menu-bar job that never takes the serve lock, both pointing at the stable installed executable. Each job's label is per data directory: `fun.tnkr.netkeep`, and the menu-bar job adds `.menubar`. The formula declares no `brew services` block. Two jobs that run `serve` would race for the serve lock."
- **Rule 9 becomes:** "netkeep never updates itself silently. The UI may check the newest published GitHub release at most daily, with a setting to turn it off. It shows the command. The UI and the menu bar may offer an Update action that runs `netkeep update` only after you confirm (ADR 0014)."

## Consequences

- You see netkeep's state, and reach its common actions, without opening its window.
- We maintain an AppKit process, a second launchd job, a preference, and a server-side roll-up.
- **Keep true:** the menu bar never acts on its own, keeps the key only in memory, and calls nothing outside rule 4's list.
- **Checked on a real Mac** (macOS 26.6.2, Apple silicon, 2026-10-25, #760): the process survives `uv tool install`; Gatekeeper leaves the bundle alone; hiding works but isn't reported to the process; a script or link that runs Python shows as "python3.12" and can't post notifications, so the amendments below change the helper's executable. [The menu-bar app](../design/menu-bar.md) records each result.

## Amendments (2026-10-25, #760)

- **The helper's executable is a copy of the installed tool's Python**, named for the helper and ad-hoc signed with the helper's bundle id. `Contents/lib` links to that Python's `lib`. The launchd job passes the arguments that run `netkeep menubar`. *Reason:* macOS names a process after its executable file, so a script that runs Python, or a link to it, shows as "python3.12" in System Settings, and notifications fail.
- **12. `setup` and `update --finish` rewrite the copy only when the installed Python changed.** *Reason:* macOS treats each rewrite as a new program, so it asks for the Keychain item again. The first start asks once, naming the helper; **Always Allow** covers later starts. Rule 3 still covers a refusal.
- **Rule 9:** macOS doesn't tell the process that you hid its item. The preference's help text says that hiding it in System Settings leaves it running.
- **Rule 10:** a notification from the helper arrives under its own name and icon. 1.0 still has none; adding them needs an amendment.
- **Open:** System Settings shows the helper without an icon. #766 checks it from `~/Applications`.

Sources: PyPI's JSON for pyobjc-core, pyobjc-framework-Cocoa, and rumps (checked 2026-10-22); [NSApplication.ActivationPolicy](https://developer.apple.com/documentation/appkit/nsapplication/activationpolicy-swift.enum); [LSUIElement](https://developer.apple.com/documentation/bundleresources/information-property-list/lsuielement); [UserNotifications from a command-line program](https://developer.apple.com/forums/thread/724249); [desktop-notifier's macOS backend](https://desktop-notifier.readthedocs.io/en/latest/_modules/desktop_notifier/macos.html); [osascript and Script Editor's permission](https://forum.latenightsw.com/t/trying-to-use-terminal-for-display-notification/5068); [macOS Tahoe's menu-bar settings (OWC)](https://eshop.macsales.com/blog/97824-macos-tahoe-makes-it-extremely-easy-to-clean-up-a-messy-menu-bar/); [Gatekeeper](https://support.apple.com/guide/security/gatekeeper-and-runtime-protection-sec5599b66df/web).
