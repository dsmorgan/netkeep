# Architecture decision records

An ADR records one decision, the context that forced it, and its consequences. They exist so a contributor who arrives later can tell a deliberate choice from an accident.

The project was called netkeeper until 2026-10-19, when it became netkeep (#661). The ADRs written before then say netkeeper and mean netkeep: their text and file names stay as written, and so do the module paths they name (`netkeeper/...` is now `netkeep/...`).

## Rules

- **One decision per ADR, under about 1,000 words before amendments.** State the decision in a paragraph, list the rules that make it acceptable with a one-line reason each, list the consequences, and stop. If yours runs much longer, you're writing design, and design belongs in [architecture.md](../architecture.md) or a design document under `docs/` that the ADR links to. The spec holds the design; the ADR holds the reasoning.
- **The decision is never changed after acceptance.** A reversal is a new ADR that supersedes the old one. Condensing an accepted ADR's prose, or moving its design detail out to a document it links to, is allowed when the decision reads the same afterward; record it as an amendment with the date.
- **Amendments are one line each**, under `## Amendments`, dated, with the issue number: what changed and why, in a sentence or two. The mechanics go in the design document, not in the amendment.
- **Numbers, locator rules, CDP methods, error strings, and test listings don't belong in an ADR.** They drift, and a reader looking for the decision has to wade through them. Put them where they're tested, and link.
- These rules apply to new ADRs and new amendments. Older amendments that carry numbers (ADRs 0003 and 0004) stay as written.
- Propose one by opening an issue, then a pull request that adds `NNNN-short-title.md` from the [template](template.md).

| ADR | Title | Status |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0002](0002-attach-only-browser-mode.md) | Attach-only browser mode for LinkedIn | Accepted |
| [0003](0003-gmail-api-over-smtp.md) | Gmail API with OAuth instead of SMTP | Accepted |
| [0004](0004-manual-linkedin-sends-by-default.md) | LinkedIn messages are prefilled, not sent, by default | Accepted |
| [0005](0005-user-boundary-from-the-first-migration.md) | Carry a user boundary from the first migration | Accepted |
| [0006](0006-observe-dont-request.md) | Observe, don't request | Accepted |
| [0007](0007-prefill-inputs.md) | The prefill's inputs: one Message click, typing into one verified composer, never Enter | Accepted |
| [0008](0008-auto-send.md) | Auto-send clicks Send, only from the scheduler, and stops at the first doubt | Accepted |
| [0009](0009-gmail-deletes.md) | netkeeper deletes nothing in Gmail; a future trash switch follows fixed rules | Accepted |
| [0010](0010-contact-channel-preference.md) | Contact channel preference: you set it, a campaign skips a mismatch unless you override, and routing comes later | Accepted |
| [0011](0011-installation-and-updates.md) | Installation and updates: PyPI and uv first, a one-line installer in front, Homebrew next, a signed app only if needed | Accepted |
| [0012](0012-chrome-profiles.md) | Chrome profiles: one netkeeper Chrome for the UI, LinkedIn, and setup | Accepted |
| [0013](0013-local-api-secret.md) | A per-install key for the local API, handed to the browser as a cookie | Accepted |

The mechanics behind ADRs 0006, 0007, and 0008 (the browser inputs, their checks, and their pins) are in [How netkeep drives a LinkedIn page](../linkedin-browser-inputs.md).
