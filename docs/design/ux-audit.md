# UX audit and information architecture proposal

Input for the polish design sessions (#604, feeding #605 and #610). It maps today's UI, walks four journeys through it, lists the gaps, and proposes a navigation, a home page and a first-run flow. The design sessions make the decisions; nothing here is decided.

Every name, company, address and message in this document and its mockups is invented.

**How this was made.** A read of `frontend/src/` and `netkeeper/web/api/`, plus a run of `netkeeper serve` on a scratch data directory with 40 invented contacts imported from CSV, 26 of them triaged, three templates, a list, and two draft campaigns. Screenshots of every route at desktop width, and of two routes at phone width and in dark mode, went to the design session with the mockups. They are not in the repository.

## Contents

1. [Today's UI map](#1-todays-ui-map)
2. [Journeys](#2-journeys)
3. [Gaps](#3-gaps)
4. [Proposal](#4-proposal)
5. [Mockups](#5-mockups)

## 1. Today's UI map

### 1.1 The shell

Every page has the same frame:

- **Sidebar.** Twelve items, in this order: Dashboard, Contacts, Triage, Lists, Imports, Exports, LinkedIn, Gmail, Templates, Campaigns, Inbox, Settings. They are all at the same level and none are grouped. Badges and dots come from `GET /attention`, which is polled every 60 s and refetched after each write:
  - Number badges: Dashboard (LinkedIn messages waiting for you plus retries), Triage (contacts left), Imports (draft runs), Inbox (unhandled items).
  - Amber dots: Dashboard (auto-send hold), LinkedIn (session flag or heat), Gmail (a mailbox needs sign-in again, or a leftover draft is waiting for an opted-out contact), Settings (a tripped breaker).

  The sentence that explains a dot is only in the link's screen-reader name. A sighted user sees the dot and nothing else.
- **Top bar.** It holds:
  - the page title (`<h1>`)
  - the activity indicator ("LinkedIn: running / blocked / paused / armed / off", plus "Campaigns: sending" for 15 s after each campaign tick)
  - the background-checks popover ("Gmail: checked 3 min ago · next in 7 min", "LinkedIn inbox: …", with a **Check now** for Gmail replies)
  - backend health ("Backend ok · 0.0.1")
- **Banners** above the page: Gmail needs sign-in again (with **Re-authorize**), and a dismissible not-Chrome warning.
- **Event stream.** One `EventSource` on `/api/v1/events`. It carries `run.started`, `run.progress`, `run.finished`, `mailbox.status`, `task.finished` and `campaign.tick`. The dashboard is the only page that shows whether it is connected.

### 1.2 Routes, cards, actions and the API behind them

All paths are under `/api/v1`.

| Route | Cards and sections | Main actions | API |
|---|---|---|---|
| `/` Dashboard | Start here (0 contacts only) · Live LinkedIn run · LinkedIn queue · Waiting for you · Next campaign sends · Next LinkedIn run · LinkedIn browser · Mailbox · Budget and heat · Replies this week · Changed jobs · five setup-step cards (Import, Review tags, Triage, Build a list, Export) | Pause / Cancel run / Resume run; Prefill, Prefill next, Try again; I sent it, check now; Discard; Pause schedule; Set up Gmail; Re-authorize; Open LinkedIn | `contacts/stats` (gates the page), `linkedin/status`, `linkedin/runs*`, `campaigns/linkedin/ready·options·waiting·prefill·messages/*`, `dashboard/next-fires·inbound·changed-jobs`, `linkedin/schedule*`, `mailboxes`, `linkedin/budget·heat`, `imports?status=draft`, `lists` |
| `/contacts` | Filter bar, Views menu, Columns, table, bulk bar | Add contact; Save view; Count the selection, then Apply (met, not met, skip, do not contact, archive) | `contacts/query`, `contacts/bulk*`, `views`, `tags`, `lists/{id}/members` |
| `/contacts/$id` | Fields · Tags · Record · Notes · Contact details · Campaigns · Timeline · Snapshots | Merge with…; Archive; Confirm / Reject and archive; Revert field; Record a response | `contacts/{id}`, `…/merge*`, `…/duplicates`, `…/emails·phones·links`, `…/tags`, `…/notes`, `…/timeline`, `…/enrollments`, `…/outside-response` |
| `/triage` | Queue filter, progress line, evidence panel, suggestion banner, queue list | m / n / s / p / t / ← / → / u / ?; Mark N as met | `triage/*`, `contacts/query`, `tags` |
| `/lists` (tabs: Lists, Tags and rules, Saved views) | List detail, Filter, Membership, Members, Bulk actions · Tags, Auto-tag rules · Saved views | Create list; Export; Delete list (no confirm); Run all rules now; New view | `lists*`, `tags*`, `autotag-rules*`, `views*`, `contacts/bulk*`, `exports` |
| `/imports` (tabs: New import, History) | Seven-step wizard: Upload, Map, Preview, Candidates, Commit, Review, Result | Choose a file; Save preset; Commit N rows | `imports/presets·inspect·archive`, `imports`, `imports/{id}/preview·commit·rows` |
| `/imports/runs`, `/imports/runs/$id` | Import history · Run detail | Finish this import; Roll back this import | `imports`, `imports/{id}`, `…/rollback` |
| `/exports` | Who to export · The file · What each preset holds | Download | `contacts/query`, `exports`, `tags` |
| `/linkedin` | Session banner · Scheduled runs · Browser · Budget · Heat · Runs (+ run detail) · Start a run · LinkedIn inbox · Pins | Arm / Disarm scheduled runs; Pause schedule; Clear flag…; Clear heat…; Start run; Stop / Resume run; Check inbox now; Pin | `linkedin/*` (every endpoint) |
| `/gmail` | Connection · Today's sends · Reply poll · Posture (four rows) · Leftover drafts · Recent email | Re-authorize; Check now; Open in Gmail | `mailboxes/status`, `gmail/activity·leftover-drafts`, `poll-status*`, `posture` |
| `/templates` | All templates · Editor (with AI draft helper and merge fields) · Preview · Version history | New template; Save; Delete; Copy prompt / Paste result; Add to dictionary | `templates*`, `templates/lint·merge-fields·dictionary`, `contacts` (search) |
| `/campaigns` | Campaigns table · Archived campaigns | New campaign; Show archived | `campaigns` |
| `/campaigns/new` | New campaign · Steps · Audience | Add a step; Save draft | `templates`, `mailboxes`, `campaigns/linkedin/options`, `lists`, `tags`, `campaigns/audience-preview`, `campaigns` |
| `/campaigns/$id` | Overview · Results · Make a list from this campaign · Steps · LinkedIn queue and Waiting for you · Audience · Review · Enrollments | Pause / Resume; Change start; End; Archive; Delete; Enroll the audience; Start review; Approve; Run lint; Test send; Activate; Use newest version; Timing; Switch to Prefill / Auto-send; Record a response | `campaigns/{id}*` (36 endpoints), `campaigns/linkedin/*`, `settings/sending-hours`, `poll-status` |
| `/inbox` | One list, with Show and Kind filters | Mark handled; Add note | `inbox`, `inbox/{id}/handled`, `contacts/{id}/interactions`, `mailboxes` |
| `/settings` | Intro · Gmail (eight-step setup guide, OAuth client, mailboxes, steps to arm) · Sending hours · About you · LinkedIn budgets · LinkedIn active hours · Campaign defaults · Backups (config) · Backup files · Do-not-send list · Posture (30 rows) · Current user (raw JSON) | Connect Gmail; Check now; Arm for drafts / Arm to send / Disarm / Disconnect; Save (×7); Back up now; Acknowledge; Reset breaker; Forget inbox owner | `gmail-setup`, `mailboxes*`, `settings/*`, `backups`, `do-not-send`, `posture`, `linkedin/schedule/reset-breaker`, `linkedin/inbox/*`, `me` |

The settings page is about 6,900 px tall at desktop width. The dashboard is about 1,900 px tall with nothing imported, and its five setup cards sit at the bottom, below eleven status cards.

### 1.3 The same thing in more than one place

| What | Where it appears | The problem |
|---|---|---|
| Gmail connection and Re-authorize | Banner, Dashboard Mailbox card, Gmail Connection card, Settings Gmail section, Settings setup guide step 7 | When a mailbox needs sign-in, you can see two Re-authorize buttons on one screen (three on Settings). Each card has its own status labels. "Connect" goes three ways: the dashboard sends you to Settings, the Inbox empty state says "Connect Gmail on the Gmail page", and the Gmail page says to use the terminal or Settings. The Gmail page can't connect at all. |
| Mailbox arm state | Dashboard, Gmail, Settings | Only Settings can change it. The campaign callout says "Arm a mailbox in Settings → Gmail". |
| Posture | Gmail page (four rows), Settings (all 30) | Auto-send state appears in five more places. |
| Gmail reply checks and their Check now | Top bar popover, Gmail Reply poll card, campaign Results card | The sentence "netkeeper serve isn't running, so nothing is checked in the background." is copied word for word. The dashboard's "Reply detection is not set up yet" doesn't use poll status at all. |
| "Check now" | Poll status: poll replies soon. Settings mailbox row: check the token and Message-ID | Same label, different actions. |
| Setup steps | Dashboard (five steps: import, tags, triage, list, export) and the Settings Gmail guide (eight steps) | Two separate ladders with different badge words. Neither covers LinkedIn Chrome or the first sync. "Start here" repeats the import step. |
| LinkedIn queue and Waiting for you | Dashboard and campaign detail | This one is fine (it's filtered per campaign), but it is the largest card on both pages, even when it's empty. |
| LinkedIn schedule | Arm and disarm on `/linkedin` only. Pause on Dashboard and `/linkedin`. State in the top bar and again inside the schedule card | `/linkedin` shows the activity indicator twice. |
| Live run controls | Dashboard: Pause, **Cancel run**, Resume. `/linkedin`: **Stop**, Resume | Same action, two labels. Pause is missing on one of them. |
| Session flag | Dashboard (text only), `/linkedin` (with Clear flag…), sidebar dot | The checkpoint copy differs between the two. |
| Budget and heat | Dashboard summary, `/linkedin` detail, Settings limits, campaign Daily cap | The profile-view notice can render up to four times on `/linkedin`. |
| Breakers | Reset on Settings › Posture only. The signal is the Settings dot and "LinkedIn: blocked" | `/linkedin`, where the breaker blocks, has no breaker state. |
| Bulk contact actions | `/contacts` ("Mark as met", "Count the selection") and `/lists` ("Set “met”", "Count first") | Different words for the same actions. |
| Saved views | `/contacts` Views menu and `/lists` Saved views tab | Two editors for one thing. |
| Export | `/lists` list detail and `/exports` | The same form in both. |
| Backend health | Top bar, Dashboard ("Start it with `make dev`"), Settings Current user | The dashboard tells an end user to run a developer command. The user guide says `serve`. |
| Chrome-only notice | Banner and Settings intro | Two wordings. |
| Page heading | Top bar `<h1>` on every page; plus a visible h1 on Templates and sr-only h1s on Gmail and LinkedIn; plus "Settings" and "Inbox" repeated as card titles | Two h1s on three pages. |

## 2. Journeys

The click counts assume a person who knows where everything is. Real first-time counts are higher.

### 2.1 First run: install, Gmail, LinkedIn Chrome, first sync, first import

| Step | Today | Clicks or commands | Friction |
|---|---|---|---|
| Install | Terminal: clone, `make install`, `make build-ui`, `netkeeper serve`, open Chrome at 127.0.0.1:8000 | 4 commands | Out of scope for the UI (#594, #596). The UI's first sight is a dashboard of eleven empty cards. |
| Orientation | Dashboard | 0 | "Start here" says to import, but the page doesn't link it until the setup cards at the bottom. The LinkedIn queue, the largest card, warns about an inbox poll the user has never heard of, and offers "Check inbox now" before any Chrome exists. "Build a list" already reads "1 list", because the built-in Validated list counts. |
| Import | Sidebar › Imports, Choose a file, Import, See your contacts | 4 | Works well for the LinkedIn zip. The entry label varies: "Import contacts", "Import a CSV", "New import". |
| Gmail | Dashboard › Set up Gmail (lands on Settings), then the eight-step guide inside a 6,900 px page, then Google console in another tab, then back | 8 marks plus 3 forms plus OAuth | The guide is good, but it's buried as the first card of Settings with the rest of Settings below. Nothing afterwards says "leave it disarmed" in the flow itself. Arming is a separate trip to the same card. |
| LinkedIn Chrome | `/linkedin` › Browser card: copy a command into Terminal, sign in to LinkedIn, then run `netkeeper preflight` in Terminal | 2 commands | The UI can't check that it worked. "Last known session: unknown — not checked yet" stays until a run or preflight. Nothing tells the user this window is separate from their everyday Chrome until they read the card. |
| First sync | `/linkedin` › Start a run: pick Full connections sync, Start run, confirm. Then LinkedIn inbox › Check inbox now. Then Scheduled runs › Arm, confirm | 7 | Order matters (sync, then inbox, then arm) but nothing sequences it. Scheduled job names are raw keys (`connections_incremental: every 24h`). |
| Triage | Sidebar › Triage, keys | 1 plus keys | Good. The badge (14) is the only pointer to it. |

A CSV-only user hits a wall later: our 12 CSV contacts couldn't join a LinkedIn campaign. "0 will start, 12 skipped (12 no LinkedIn member id yet (a connections sync adds it))". The first-run flow should say that a sync is what makes imported contacts reachable on LinkedIn.

### 2.2 Daily use: what's running, what needs me, replies

| Question | Where you answer it today | Clicks | Friction |
|---|---|---|---|
| Is anything running? | Top bar chips plus the Dashboard Live run card | 0–1 | Spread across the top bar, the dashboard, `/linkedin` and the favicon. There is no single "now" area. |
| What needs me? | Sidebar badges (Dashboard, Triage, Imports, Inbox) plus four amber dots | 1 per badge | No ranked list. A dot doesn't say why. A tripped breaker shows as a dot on **Settings**. |
| Did anyone reply? | Inbox, or Dashboard "Replies this week" | 1 | The dashboard card counts inbound messages, not unhandled replies, and doesn't link to Inbox. |
| LinkedIn messages to send | Dashboard › Waiting for you, then switch to the LinkedIn Chrome, Send, come back, then I sent it, check now | 3 per message | Fine once learned. Dashboard is the only place, and it's the Dashboard badge, not the Inbox badge. |
| What's next? | Dashboard › Next campaign sends and Next LinkedIn run | 0 | Two cards with different shapes. No timeline. |

### 2.3 Running a campaign

From templates to the first send:

| Step | Clicks | Friction |
|---|---|---|
| Write templates (`/templates`, New template, fill in, Create) | 4 per template | Fine. |
| Sending hours (Settings, scroll to Sending hours, Save) | 3 | It's in the middle of Settings, far from campaigns. |
| New campaign (Campaigns, New campaign, name, mailbox, steps, audience, Save draft) | about 8 | The mailbox select says "No mailbox (LinkedIn steps only)" with no way to connect one from here. |
| Arm the mailbox for drafts (Settings › Gmail › Arm for drafts) | 3 | You have to leave the campaign. |
| Enroll (Audience › Enroll the audience) | 1 | The skip reasons are good. |
| Review (Start review, Run lint, Approve each step, Test send, Activate, confirm) | 6 or more | The Review checklist sits under the Audience card, and the page has eight stacked cards. A draft shows Overview, Steps, Audience, Review and Enrollments. An active campaign shows Overview, Results, Make a list, Steps, LinkedIn queue and Enrollments. Nothing says "do this next". The review list mixes done and missing items with gate vocabulary ("Personal-line messages approved one by one: done", for a campaign with no personal line). |
| Arm to send (Settings › Gmail › Arm to send, after a test) | 3 | Another trip to Settings. |
| Watch results (Campaign › Results) | 1 | Good. |

That is about 30 clicks and three trips to Settings. The callout "Email replies aren't watched … Arm a mailbox in Settings → Gmail" appears on a LinkedIn-only draft, before it can matter.

### 2.4 Fixing a problem: posture, breakers, support bundle

| Problem | Today | Friction |
|---|---|---|
| A breaker tripped | "LinkedIn: blocked" in the top bar, a dot on Settings. Then Settings, scroll about 6,000 px to Posture, find the row, Reset breaker, confirm | The signal and the fix are on different pages from where the user looks (`/linkedin`). The popover's "needs attention" doesn't link anywhere. |
| Session flag (checkpoint, logged out) | Banner on `/linkedin`, Clear flag…, confirm | Fine. The dashboard card has no Clear. |
| Heat | `/linkedin` › Heat › Clear heat… | Fine. |
| Gmail token expired | Global banner › Re-authorize | Good. It only re-authorizes the first stuck mailbox. |
| "Is everything OK?" | Settings › Posture: 30 rows in one table | No summary, no grouping by severity. "NOT clear: 1 of 30 protections is not in force, 1 warning in all" is at the bottom. |
| Logs | None in the UI. `<data_dir>/logs/serve.log` | No endpoint. |
| Support bundle | Doesn't exist | — |
| Restore a backup | Settings shows the command. CLI with serve stopped | By design. Fine. |

## 3. Gaps

### 3.1 Backend can, UI can't

Endpoints with no frontend caller (two `/tasks` demo endpoints and the OAuth callback left out):

- `POST /autotag-rules/{id}/run`: re-run one tag's rules.
- `GET /campaigns/{id}/mode-changes`: the history of prefill and auto-send switches.
- `PATCH /contacts/{id}/phones/{id}` and `PATCH /contacts/{id}/links/{id}`: edit, not only add and delete.
- `GET`, `PATCH` and `DELETE` on `/contacts/{id}/interactions` and `/interactions/{id}`: edit or delete a logged note, call or meeting.
- `POST /do-not-send`: add an address by hand (CLI only).
- `DELETE /imports/presets/{name}` and `DELETE /imports/{id}`: delete a saved preset or a draft run. The history lists drafts but can't remove them.
- All of `/me/positions`: your own job history.
- `GET` and `DELETE /templates/dictionary`: list and remove dictionary words.

CLI-only capabilities a UI user would want:

- `history import` and `history scan-gmail` (#601 is adding the UI)
- `preflight`, a live Chrome check
- `config adopt`
- `linkedin message-check`
- `linkedin field-changes`
- `linkedin unreadable` across runs
- `simulate`
- `do-not-send add`
- `import rm`
- `templates dictionary list` and `remove`

`docs/guide/troubleshooting.md` still lists `reset-breaker` and `inbox-forget-owner` as CLI-only. #583 added both to the UI.

### 3.2 UI needs, backend lacks (for #610)

1. **One setup-state endpoint.** Today the dashboard derives setup from `contacts/stats`, `imports?status=draft` and `lists`, and the Gmail guide has its own `gmail-setup`. A first-run flow needs one answer: about you, contacts, Gmail connected and armed, Chrome reachable and signed in, first full sync, first inbox poll, scheduled runs armed, first triage. Each with done, skipped or blocked, and why.
2. **A live Chrome check the UI can call.** Something like `POST /linkedin/browser/check`: attach, read the session, detach, as a run with its own lock and budget. `GET /linkedin/browser/health` is deliberately never a probe. The first-run "Check connection" button needs this, or the step stays "run `netkeeper preflight`".
3. **A ranked "needs you" feed.** `GET /attention` gives counts and flags. Home needs items: kind, sentence, target, severity, created_at. That covers replies, waiting prefills, retries, a review in progress, a tripped breaker, a session flag, mailbox reauth, leftover drafts, draft imports and triage.
4. **An "upcoming" feed** that merges `dashboard/next-fires`, the scheduled jobs and poll status into one time-ordered list.
5. **A health summary**: posture grouped into problems, warnings and on, each problem with its fix action. `GET /posture` has `actions` per row; it needs a severity and a plain-language fix sentence.
6. **Logs**: the last N lines of `serve.log`, filtered and redacted.
7. **A support bundle**: a zip of the config (secrets removed), versions, posture, recent runs and log tail, with message bodies, emails and cookies removed.
8. **A "connect Gmail" entry that works from anywhere**: the OAuth start already exists. What's missing is a return target, so the flow comes back to the page it started from instead of `/settings`.
9. **"Skipped" per setup step**, stored per user, so a skipped step stays a reminder instead of a nag.

### 3.3 Inconsistent copy and states

| Concept | Words in use today | Suggest |
|---|---|---|
| Replies | Inbox, Replies this week, inbound messages, Reply | **Inbox** for the place, **replies** for the items |
| Checking for replies | Reply poll, Gmail replies, mailbox sync, mailbox poll, reply detection, background checks. On LinkedIn: inbox poll, inbox check, Check inbox now, LinkedIn inbox | **Reply check** (Gmail) and **LinkedIn inbox check**; "Background checks" for the group |
| Review | Triage "Reviewing", contact "Needs review", "Review what was tagged", import Review, campaign Review, "waiting for your review" | Keep **Review** for campaigns. Use **Confirm** for contacts read off a card, **Check** for the triage pass over automatic decisions, **Tags** for the tag step |
| Stopped | blocked, tripped breaker, paused, held, stopped, needs attention, aborted | **Paused** = a person did it. **Stopped** = a safety check did it, and it always comes with the reason and the fix. Retire "blocked" in the UI |
| Armed | LinkedIn schedule armed/disarmed/off, mailbox not armed / drafts only / sending | LinkedIn: **Scheduled runs on/off**. Gmail: **Sending: off / drafts only / on**. One word should not mean two switches |
| Stop a run | Cancel run (Dashboard), Stop (LinkedIn) | **Stop run** everywhere |
| Re-authorize | Re-authorize, reauthorize, sign in again | **Sign in again** (the button), "Gmail needs you to sign in again" (the state) |
| Import entry | Import contacts, Import a CSV, New import | **Import contacts** (it takes a zip or a CSV) |
| Job names | `connections_incremental` (LinkedIn page) vs "Incremental connections sync" (Dashboard) | Labels everywhere |
| Hours | Sending hours (email), LinkedIn active hours, "outside active hours", "Outside the sending hours" | Keep both terms but label each with its channel |
| Loading | Loading…, Checking…, Loading the X…, Checking your setup… | "Loading X…" |
| Errors | raw `error.message`, "X could not be loaded.", "The X is unavailable.", "Could not load the X" | "X couldn't load." plus Try again, with detail behind a disclosure |
| Destructive confirms | Delete list has none; campaign, template and do-not-send deletes do | Confirm every delete |
| Developer copy in the UI | "Start it with `make dev`", raw `GET /me` JSON on Settings, scratch paths in config hints | Remove from user-facing pages, or move under a Developer disclosure |

## 4. Proposal

### 4.1 Principles

1. **One home answers two questions:** what is happening now, and what should I do next. Everything else is a place you go to work.
2. **Every warning carries its fix, where you see it.** No dot without a sentence, no "blocked" without a button.
3. **Each fact has one owner page.** Other pages show at most a one-line summary that links to it.
4. **Setup is a flow, not a settings page.** It runs once, can be resumed, and remembers what you skipped.
5. **Settings holds preferences only.** Connections, health and setup move out.

### 4.2 Navigation

Seven items instead of twelve: five primary, two utility.

| Item | Contains (today's pages) | Badge |
|---|---|---|
| **Home** | New. Replaces Dashboard | — |
| **Inbox** | Inbox, plus LinkedIn messages waiting for you to send | replies plus waiting messages |
| **Contacts** | Tabs: All (Contacts) · Triage · Lists and tags (Lists, Tags and rules, Saved views) · Imports and exports | contacts to triage |
| **Campaigns** | Tabs: Campaigns · Templates | campaigns in review |
| **Connections** | Tabs: Gmail (connection, sending, today's sends, leftover drafts, recent email) · LinkedIn (Chrome, scheduled runs, runs, budget, heat, pins) | — |
| *Health* | Posture, breakers, background checks, backups, logs, support bundle | problems (red), warnings (amber dot with text) |
| *Settings* | Preferences only: about you, sending hours, LinkedIn budgets and hours, campaign defaults, do-not-send list, backup schedule | — |

On a phone the sidebar becomes a bottom tab bar: Home, Inbox, Contacts, Campaigns, More (Connections, Health, Settings). The top bar keeps a single status chip, such as "Run going", that opens Home's "Happening now" panel.

Where things move:

- Setup becomes `/setup`, reachable from Home until it's done.
- The Gmail setup guide moves from Settings to Setup and Connections › Gmail.
- Posture and Backup files move from Settings to Health.
- The top-bar background-checks popover becomes a Health section, with a one-line summary on Home.
- The Exports page becomes a dialog from any contact selection or list, plus Contacts › Imports and exports.
- The saved-views editor merges into the Contacts table's Views menu.

### 4.3 Home

Top to bottom (see `home.html`):

1. **Greeting line** with the date, whether the sending window is open, and how many things need you. On the right, three chips: Gmail, LinkedIn, problems. Each opens its owner page.
2. **Setup bar** while setup is unfinished: "Setup: 6 of 7 done. Next: arm Gmail to send", with Continue and Hide.
3. **Happening now.** Up to four live tiles:
   - the LinkedIn run in progress (with progress and time left)
   - Gmail sending (next send, today's count against the cap)
   - reply checks (last and next, for each channel)
   - anything stopped (with "Fix")

   This replaces Live run, Next LinkedIn run, LinkedIn browser, Mailbox, Budget and heat, and the top-bar popover.
4. **Do next.** One ranked list, at most about seven items, each with a sentence and one primary verb. The order:
   1. problems that stop work
   2. replies
   3. LinkedIn messages waiting in Chrome
   4. campaigns half-way through review
   5. draft imports
   6. triage
   7. changed jobs worth a note

   Done items strike through, and an Undo lives in the Inbox. Empty: "You are all caught up."
5. **Coming up.** One timeline merging campaign sends, scheduled runs and checks, with a stopped item marked in place.
6. **Campaigns.** One row per active or in-review campaign, with a sent and replied bar.
7. **This week.** Sent, replied, bounced, changed jobs.

The LinkedIn queue's long explanation ("Prefill types the message into a message bubble…") moves to the first time you see a waiting message, and then into a help disclosure.

### 4.4 First-run flow

Seven steps on `/setup` (see `first-run.html`). Steps 4 to 7 are optional and skippable. A skipped step stays as a reminder on Home's setup bar.

1. **Welcome.** What netkeeper does, and what it never does: never starts Chrome or signs in for you, never deletes in Gmail, never sends before review, never goes over budget.
2. **About you.** Name, company, time zone. These are templates' merge fields, and netkeeper never sends to this contact.
3. **Bring in contacts.** Drop a zip or a CSV, see what was recognized, and import. Possible duplicates can be decided now or later. Mentions rollback.
4. **Connect Gmail.** The six console steps collapsed into a checklist beside a link to the guide, then paste the client, then Connect. It ends with "Sending stays off. Campaigns only make drafts until you arm the mailbox, after a test."
5. **LinkedIn Chrome.** The launch command with Copy, "sign in in that window", then **Check connection**. This needs the live-check endpoint (§3.2 item 2). Until then the step shows the `netkeeper preflight` command.
6. **First sync.** Start the full connections sync and watch its progress inline. The first LinkedIn inbox check runs on its own when the sync ends. It explains that the sync is what makes imported contacts reachable on LinkedIn. It is blocked, with a link back, if step 5 was skipped.
7. **Triage a few.** Five cards with m, n and s, then "the rest wait in Contacts › Triage".

The flow then offers **Start your first campaign** or **Go to Home**. Arming scheduled runs and arming Gmail to send stay deliberate acts on Connections, and Home's setup bar points to them. They should not be steps a user clicks through while learning the app.

### 4.5 Campaign page

See `campaign.html`.

- **Header.** Status and a five-stage lifecycle bar: Draft, Enroll, Review, Active, Done.
- **One "Next" box** with the single action that moves the campaign forward: "Approve step 1: 12 LinkedIn messages", "Send yourself a test", "Activate". When nothing is needed it says what happens next and when.
- **Tabs** instead of eight stacked cards:
  - **Review** (drafts) or **Activity** (active): the gate checklist, or the LinkedIn queue scoped to the campaign.
  - **Steps**
  - **Audience** (enrolled and skipped, with reasons)
  - **Results**, including "make a list from an outcome"
- **Gate items** show only what applies. A LinkedIn-only campaign has no test-send or mailbox row.
- **The mailbox arm action** appears inline when the campaign needs it, instead of "go to Settings → Gmail".

### 4.6 Health page

See `health.html`.

- A summary row: problems, warnings, protections on, last backup.
- **Needs fixing**: each problem in plain words, why it stopped, and its fix button. For example, "I fixed it, resume checks" resets the breaker, with "forget the recorded account" as the alternative.
- **Worth a look**: warnings, such as leftover drafts and high heat.
- **Connections** and **Background checks** at a glance, with Check now in one place.
- **All 30 protections**, collapsed.
- **Backups** (Back up now, Restore… with the command) and **Get help** (recent log, support bundle). The help actions need the backend items in §3.2.

## 5. Mockups

The mockups are clickable, self-contained HTML files with no network requests and invented data. They were handed to the design session; they are not in the repository.

| File | What it shows |
|---|---|
| `index.html` | Entry page and a summary of the directions |
| `home.html` | §4.3 Home. Mark handled and I sent them work |
| `first-run.html` | §4.4. Each step is clickable; file pick, Gmail connect, Chrome check, sync progress and triage keys are simulated |
| `campaign.html` | §4.5. Approve, test and activate walk the lifecycle; the tabs switch |
| `health.html` | §4.6. Resolving the problem clears it |

Each file has a mockup bar that switches between three visual directions and between Auto, Light and Dark. The choice carries across pages. Every file renders at 390 px wide with no horizontal scroll.

- **A · Calm.** Today's teal and neutrals with more whitespace and soft cards. The smallest change from the current UI.
- **B · Bold.** Status-forward: dark navigation, filled live tiles, a colored left edge by state, heavier type. Strongest at "what's happening".
- **C · Editorial.** Warm paper tones, serif headings, rules instead of boxes, numbered lists. Reads like a daily brief.

Questions for the session:

1. Is Inbox the right home for LinkedIn messages waiting to send, or do they stay a Home-only item?
2. Should Connections merge into Health, giving six items?
3. Is Templates a tab of Campaigns, or its own item?
4. Which direction, or which mix (for example A's palette with B's status tiles)?
5. Which §3.2 backend items are in scope for 1.0? Setup state and the live Chrome check decide whether first run can be fully in the UI.
