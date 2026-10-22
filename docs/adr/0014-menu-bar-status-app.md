# 0014. A menu-bar status app in the locally built netkeep.app

Date: 2026-10-22

## Status

Proposed (#755)

## Context

ADR 0011 left a menu-bar icon to a new ADR. On 2026-10-22 the maintainer agreed to build one without the Apple Developer Program (option T1). It builds on the launcher bundle (#754), which `netkeep setup` writes to `~/Applications/netkeep.app`, and on the three status chips (#751), which roll up on one severity scale where the worst part wins.

The facts the decision rests on:

- **PyObjC installs from wheels on both architectures.** pyobjc-core and pyobjc-framework-Cocoa 12.2.2 ship a `cp312` universal2 wheel, so ADR 0011's pinned install compiles nothing on Apple silicon or Intel.
- **rumps is barely maintained.** Its last release, 0.4.0, dates from October 2022, and it ships only an sdist.
- **Any Python process can own an `NSStatusItem`.** An accessory activation policy keeps the process out of the Dock; `LSUIElement` does the same from a bundle.
- **Notifications need a real bundle.** `UNUserNotificationCenter` throws in an unbundled process. `osascript display notification` works anywhere, but macOS shows the notification as Script Editor's, and only after you allow Script Editor's notifications.
- **macOS 26 lets you hide any app's menu-bar item.**
- **Gatekeeper assesses only quarantined files,** and a bundle written on the Mac isn't quarantined.

## Options

- **a. PyObjC, from a helper inside the locally built netkeep.app (chosen).** It needs no account and no third-party install.
- **b. rumps:** a thin wrapper over the same calls, with no release in four years.
- **c. A Swift or signed app:** it needs the Apple Developer Program, which ADR 0011 declined.
- **d. SwiftBar or xbar:** you'd have to install and trust a third-party app.
- **e. No menu bar:** you'd see the chips only while the window is open.

## Decision

Option a. `netkeep setup` adds a helper bundle inside `netkeep.app`, with its own bundle id and `LSUIElement`, so clicking the launcher never lands on the menu-bar process instead. The helper runs the installed `netkeep menubar`, a PyObjC `NSStatusItem` with an accessory activation policy. The process shows one icon, tinted by the worst of the three chips, and a menu: the three chip lines, **Open netkeep**, **Check replies now**, **Pause** or **Resume scheduled runs**, **Update to X.Y.Z** (only when the daily check found one), and **Quit netkeep**. It sends no notifications in 1.0.

### Rules

1. **One roll-up, on the server.** A read-only endpoint returns each chip's tone, word, and fact, and both the UI and the menu bar read it. *Reason:* two copies of #751's rules would drift.
2. **It's a local client.** It sends `Authorization: Bearer <key>` with the key from the Keychain (ADR 0013), and only to its own data directory's `serve` on loopback. *Reason:* no cookie and no exemption.
3. **When the Keychain refuses, the icon stays and says so.** The icon turns to the "off" tone, and the menu explains why. It retries only when you open the menu, so prompts never pile up. **Open netkeep** and **Quit netkeep** still work. *Reason:* a dead icon is a dead end.
4. **It acts only on your click, from a fixed list:** `netkeep open`, the replies check, schedule pause and unpause, `netkeep update`, and the login item's stop. It never arms anything, starts a run, sends, changes a setting, or launches Chrome except through `netkeep open`. A test pins the list. *Reason:* a background process that can start and stop things does only what you ask.
5. **Update runs on your click and shows its progress.** The menu bar starts `netkeep update` detached, so the update outlives it, and shows each step and any refusal. `update --finish` restarts the menu bar last, on the new code. *Reason:* ADR 0011 rule 9 forbids a silent update, not a deliberate one.
6. **Quit asks first, then stops `serve` through the login item** until the next login or **Open netkeep**. *Reason:* "Quit" should mean that nothing is running.
7. **One switch, two launchd jobs.** **Run at login** and `netkeep setup` add and remove both jobs together, from one implementation, and the menu-bar job never takes the serve lock. *Reason:* `update` stops `serve` while the menu bar stays up to show progress. ADR 0011 rule 6 prevents two jobs racing for the serve lock, and that race can't happen here.
8. **It survives `serve` restarts.** While `serve` is down, the icon shows "Unreachable", or "Updating" during an update. It never starts `serve` on its own. launchd restarts it after a crash, not after Quit. *Reason:* only you and the login item start `serve`.
9. **Hiding the icon is your choice.** netkeep doesn't fight macOS 26's setting, and a **Show menu-bar icon** switch in Settings removes the job. *Reason:* netkeep works fully without the menu bar, and a hidden icon shouldn't leave a process running.
10. **No notifications in 1.0.** The icon's tint is the signal. *Reason:* a notification from "Script Editor", after a permission prompt for Script Editor, confuses more than it helps. A later ADR can add notifications if the helper turns out able to post them.
11. **PyObjC is a macOS-only dependency** in the release's constraints file, and the Intel CI job imports it. *Reason:* Linux and the container don't need it.

## Consequences

- You see netkeep's state, and reach its common actions, without opening its window.
- We maintain an AppKit process, a second launchd job, and a server-side roll-up that the UI moves onto.
- **Keep true:** the menu bar never acts on its own, keeps the key only in memory, and calls nothing outside rule 4's list.
- **Unverified; check on a real Mac before implementation:**
  - whether the helper's process, which runs the uv-installed Python, keeps the helper's identity, or shows up as "Python" in macOS 26's menu-bar list;
  - whether reading the Keychain item that `serve` created prompts from the helper;
  - whether macOS 26 tells the process that you hid its item;
  - whether `UNUserNotificationCenter` works from an ad-hoc-signed helper;
  - whether Gatekeeper leaves the locally built bundle alone (#754 checks this for the launcher).

Sources: PyPI's JSON for pyobjc-core, pyobjc-framework-Cocoa, and rumps (checked 2026-10-22); [NSApplication.ActivationPolicy](https://developer.apple.com/documentation/appkit/nsapplication/activationpolicy-swift.enum); [LSUIElement](https://developer.apple.com/documentation/bundleresources/information-property-list/lsuielement); [UserNotifications from a command-line program](https://developer.apple.com/forums/thread/724249); [desktop-notifier's macOS backend](https://desktop-notifier.readthedocs.io/en/latest/_modules/desktop_notifier/macos.html); [osascript and Script Editor's permission](https://forum.latenightsw.com/t/trying-to-use-terminal-for-display-notification/5068); [macOS 26's Allow in the Menu Bar](https://forum.parallels.com/threads/asking-how-to-keep-toolbox-icon-on-menu-bar.369594/); [Gatekeeper](https://support.apple.com/guide/security/gatekeeper-and-runtime-protection-sec5599b66df/web).
