# Contributing to netkeep

Thanks for your interest. This page explains how the project works so your time is well spent.

## Before you start

- Read the [architecture spec](docs/architecture.md). It is the source of truth for scope, data model, and the safety rules around the LinkedIn sidecar and Gmail.
- Read the [architecture decision records](docs/adr/). If your change reverses one of them, open an issue first and propose a new ADR. Discussion is welcome; silent reversals are not.
- Check the [non-goals](docs/architecture.md#non-goals). Features outside them need an issue and a maintainer's agreement before code.

## Picking something to build

The [implementation guide](docs/implementation-guide.md) is the backlog: every work item there is, or will be, an issue with the item ID in its title. Pick an open one whose dependencies are closed, comment that you are taking it, and go. Items labeled `checkpoint` are for the maintainer.

## How to propose work

1. **Bugs:** open an issue with the bug template. Include the run notes from the Settings or LinkedIn page when the bug involves the sidecar; never include cookies, tokens, or other people's contact data.
2. **Features:** open an issue with the feature template and say which stage of the [reconnect workflow](docs/networking-workflow.md) or which spec section it serves.
3. **Small fixes** (typos, docs, obvious one-liners): send a pull request directly.

## Pull requests

- Branch from `main`. Keep one change per pull request.
- Fill in the pull request template. Say what changed and why, and which spec section or ADR it relates to.
- Tests must run offline. Nothing in the test suite may touch linkedin.com, Gmail, or the Anthropic API. Browser-touching changes need the opt-in smoke suite to pass on a real Chrome; say so in the PR.
- Fixtures captured from LinkedIn responses must be sanitized: fake names, fake URNs, no real emails or phone numbers.
- Commit messages: an imperative subject line under 72 characters, a blank line, then the why. The what is in the diff.
- Add a changelog fragment in `changelog.d/` (see its README) when behavior changed. Do not edit `CHANGELOG.md` directly; it is assembled at release time.

## Development setup

Phase 0 in the spec adds the runnable scaffold. Until then there is nothing to build. When it lands, the commands in [section 16 of the spec](docs/architecture.md#16-packaging-running-and-deployment) are the setup, and this section will link to them.

## The browser smoke suite

The offline suite drives the attach path against fakes. The smoke suite drives it
against a real Chrome, which is the only way to know that Playwright still behaves as
the code assumes and that the tab netkeep opens sends the browser's own headers and
client hints. It is skipped unless you opt in, and it talks to a loopback HTTP server
this repository serves: no test in this project, opt-in or not, contacts linkedin.com.

```sh
.venv/bin/netkeep browser launch     # prints the command; run that command yourself
NETKEEP_BROWSER_TESTS=1 .venv/bin/python -m pytest tests/smoke -q
```

**Stop `netkeep serve` before you run the smoke suite.** The test suite points
`NETKEEP_DATA` at a temporary directory for every test, so the smoke tests take
their activity lock there, not in your real data directory. They cannot see a run that
`serve` holds, and running them alongside one opens a second CDP client on your Chrome.

Run it against Chrome stable in a window, not a headless one, and say which Chrome
version you used in the pull request. Run it against an isolated Chrome (its own
`--user-data-dir`, not signed in to LinkedIn) on a debug port other than 9222, and point
`NETKEEP_CDP_URL` at it: the suite refuses every port-9222 address, the default
included, because that is where your own netkeep Chrome listens. Point
`NETKEEP_SMOKE_PROFILE` at that Chrome's `--user-data-dir` too: every attach checks
that the Chrome on the port holds netkeep's profile (#624), and the suite links each
test's profile to that directory. Without it, the suite skips and names the variable. Outside
`tests/smoke/`, every test is refused a real Chrome altogether (`tests/conftest.py`,
`_no_real_browser`); a test that genuinely needs one opts out with
`@pytest.mark.real_cdp`.

## Releasing

Releases are for the maintainer. `.github/workflows/release.yml` publishes them from CI
([ADR 0011](docs/adr/0011-installation-and-updates.md), rules 1, 2 and 4): nobody
uploads to PyPI by hand, and no PyPI token exists.

**One-time setup.**

1. On PyPI, under **Your account > Publishing > Add a new pending publisher** (or the
   `netkeep` project's **Publishing** page once it exists), add a GitHub publisher:
   PyPI project name `netkeep`, owner `dsmorgan`, repository `netkeep`, workflow
   `release.yml`, environment `release`.
2. In the repository's **Settings > Environments**, create `release`. Turn on
   **Required reviewers** and add yourself, turn on **Prevent self-review** only if a
   second maintainer exists, and under **Deployment branches and tags** allow only tags
   matching `v*`.
3. In **Settings > General > Releases**, turn on **Enable release immutability**, so a
   published release's files and tag can't change.
4. In **Settings > Rules > Rulesets**, add a tag ruleset that targets `v*`, turn on
   **Restrict creations**, **Restrict updates** and **Restrict deletions**, and add
   yourself to its **Bypass list**. Only someone on the bypass list can then push,
   move or delete a release tag.

**Each release.**

1. On a branch, set the version in `pyproject.toml` and `netkeep/__init__.py`, run
   `uv lock`, and run `.venv/bin/python -m towncrier build --yes --version X.Y.Z`.
   Merge the pull request.
2. Optional: **Actions > Release > Run workflow** on `main` for a dry run. It builds,
   checks and installs everything on both Mac architectures, and publishes nothing.
3. Tag the merged commit on `main` and push the tag:
   `git tag -a vX.Y.Z -m "netkeep X.Y.Z" <commit>`, then `git push origin vX.Y.Z`.
4. Before you approve the `release` deployment, check that the run's commit (shown at
   the top of the run) is the commit you tagged on `main`. Your approval is the check
   that counts: the workflow's own main-ancestor check lives in the tagged commit's
   copy of `release.yml`, which a tag on another branch could have edited. Then approve.
   The workflow publishes to PyPI, then creates the GitHub release with the wheel, the
   sdist, `constraints.txt`, the one-line installer `install.sh` (with the release's
   version stamped in) and `SHA256SUMS`.

The workflow refuses a tag that isn't `vX.Y.Z` for the package's version, a commit that
isn't on `main`, and a version that already has a release, drafts included.

The installer, `scripts/install.sh`, pins uv's installer by version and SHA-256. To move
it to a newer uv, download `uv-installer.sh` from that uv version's GitHub release, read
it, and update `uv_version` and `uv_installer_sha256` in the script and in
`tests/test_install_script.py`. The repository's copy has a placeholder for the netkeep
version and refuses to run; the release workflow stamps the version in.

If only the **GitHub release** job fails, the PyPI upload is good and the version is not
lost: delete any draft release the job left for the tag (**Releases**, or
`gh release delete vX.Y.Z`, which keeps the tag), then re-run that failed job. If
anything else is wrong after the PyPI upload, don't reuse the version: PyPI never
accepts the same file twice, so release the next patch version.

## AI-assisted contributions

Contributions written with AI tools are welcome. You are the author: review what you submit, make sure it follows the spec and the ADRs, and be ready to explain it in review. A pull request the submitter cannot explain will be closed.

## Safety rules that reviewers enforce

These come straight from the spec and from production incidents on the sibling project the sidecar design is based on:

- Only `attach` browser mode. No code path may launch a second browser identity.
- No `await` on browser work inside a request handler.
- Every browser-touching path takes the activity lock for its LinkedIn account.
- Nothing under `linkedin/` imports ORM models or opens a database session.
- Every query against a user-owned table goes through the scoping helper, and every list endpoint has an isolation test.
- A security checkpoint is never retried.
- Budgets are enforced between units of work, never mid-unit.
- Every protection defaults to on, and the posture page highlights anything that is off.
- Message bodies, cookies, and tokens never appear in logs.
- Migrations run on SQLite and PostgreSQL in CI; a construct that only one of them accepts is a CI failure.

## Which pull requests need the maintainer's approval

The test is the direction of the change, not the area it touches. A pull request
needs the maintainer's `approved` comment on its head commit when it can make netkeep
do more, or do something new, toward LinkedIn, Gmail, or your machine:

- A new or changed send, click, keystroke, or navigation target.
- A change to when sends happen or how many go out: timing, budgets, sending hours,
  retries.
- Loosening or removing a refusal, a guard, or a pin in `tests/http_send_sites.txt`.
- New access: an OAuth scope, a Keychain entry, a LaunchAgent, or a Chrome launch or
  attach.
- An ADR, and publishing a release.

These merge after the review agent's review and green CI, without the maintainer:

- A change that only refuses more, skips earlier, or checks harder.
- Test-only hardening of a pin or a guard.
- A refactor of a pinned function that keeps its behavior.
- Read-only parsing, error handling and messages, and crash fixes.
- Docs, UI that doesn't start an action, and dependency updates (except the
  `cryptography` split in [ADR 0011](docs/adr/0011-installation-and-updates.md)).

A change in the second group still needs its review to say "tightens only" or "no
behavior change" and give the evidence: the test that pins the old behavior, or the
diff that shows the new refusal. If the review can't, the pull request goes to the
maintainer. A safety-area pull request merged this way gets the `safety-tightening`
label and a one-line comment saying why, so the maintainer can audit it later. A
docs-only or test-only commit pushed after an approval doesn't need a new one; any
product-code commit does.

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). Be kind, be direct, assume good intent.
