# Your first campaign

A campaign sends a sequence of steps to an audience: for example, an email, a follow-up a week later in the same thread, and a LinkedIn message after that. Each step skips anyone who has replied. This guide builds one, reviews it, and starts it.

Before you start, finish [Setup](setup.md): a connected Gmail mailbox, imported contacts, and a list to send to. A first batch of about 100 people works well.

## 1. Write the templates

Open **Templates** and choose **New template**. Pick a **Channel** (**Email** or **LinkedIn message**), then give it a **Name**, a **Subject** (email only), and a **Body**. Choose **Create template**.

- Merge fields such as `{{ preferred_name }}` fill in per contact. The merge-field helper lists every field; **Preview** shows the message for a contact you pick. `{{ preferred_name }}` is the name you set in triage with `p`, or their first name.
- Lint checks the template as you type. A spelling warning for a word you mean, such as a company name, clears when you choose **Add to dictionary**.
- **Draft with your AI assistant** builds a prompt you paste into an AI chat you already use. It sends no contact data anywhere. See [Draft templates with your AI assistant](../ai-drafting.md).

Write one template per step.

## 2. Set the sending hours

On **Settings**, under **Sending hours**, choose the days and hours email may go out. Steps that come due outside them wait. Only a campaign's start ignores them.

## 3. Build the campaign

1. Open **Campaigns** and choose **New campaign**.
2. Give it a **Name** and pick the **Mailbox** it sends from. A campaign with only LinkedIn steps can use **No mailbox (LinkedIn steps only)**.
3. Under **Audience**, pick **A list** and the list, or **A filter**.
4. Under **Steps**, pick a **Template** for each step and a **Delay (days)**. A step's channel comes from its template. A later email step can go in the **Same thread** as an earlier one. A LinkedIn step needs a **Mode**: **Prefill** or **Auto-send** (see below). Use **Add a step** for more.
5. Choose **Save draft**.

## 4. Arm the mailbox for drafts

On **Settings**, under **Gmail**, choose **Arm for drafts**. While a mailbox is armed for drafts, every due email step becomes a Gmail draft that you send yourself, and the review's test sends land in your Drafts. [Gmail setup, step 9](../gmail-setup.md#9-arm-the-mailbox-drafts-first-then-send) has the details.

## 5. Review

On the campaign's page:

1. Choose **Enroll the audience**, then **Start review**. The guard summary says who will start and who is skipped, and why (for example, contacted in the last 30 days, or no email address).
2. **Run lint**. Fix any lint error in the template.
3. **Send a test of step 1** for each email step. The test arrives with a `[Test]` subject.
4. Page through each step's messages with `←` and `→`, then **Approve step 1** for each step. A step that uses `{{ personal_line }}` needs **Approve this message** for each message instead.
5. When the checklist says **Every requirement is met.**, choose **Activate**. Pick a **Start date and time**, or **Now**.

A change to a step or its template after you approve it undoes the approval, so review again.

## 6. While it runs

- **Arm to send.** After netkeep finds its own test draft in your mailbox, **Arm to send** in Settings lets `send` steps go out on their own, inside your daily cap. The **Campaigns: sending** chip in the header shows while it sends.
- **Replies stop the sequence.** netkeep checks the mailbox for replies; a reply ends that person's enrollment, and **Inbox** shows a badge for it.
- **Responses from elsewhere.** If someone answers by phone or in person, choose **Record a response** on their row in the campaign's enrollments, or on their contact page. It ends their enrollment as replied or opted out.
- **Pause and resume.** **Pause** on the campaign's page holds every step; **Resume** picks up again. A step that came due while paused fires at the next chance. **End campaign** stops it for good.
- **Follow up.** Under **Make a list from this campaign**, make a list from one outcome, such as the people who didn't reply, then **Start a campaign from it**.

## 7. LinkedIn steps

A LinkedIn step is prefilled by default: netkeep types the message into the conversation in your LinkedIn Chrome and stops, and you click **Send**.

1. When a step is due, it appears in the LinkedIn queue on the dashboard and on the campaign's page. Choose **Prefill**. Nothing prefills on its own.
2. netkeep opens the conversation in the LinkedIn Chrome and types the message. Read it there and click **Send** yourself.
3. The message moves to **Waiting for you** until the next inbox poll sees it sent. **I sent it, check now** runs that poll at once. **Discard** drops a prefill you won't send.

An email reply also stops a LinkedIn-only enrollment, as long as a mailbox is armed. Without one, the campaign's page says "Email replies aren't watched".

**Auto-send** clicks Send for you. It's off by default, and only `config.toml` turns it on; see [the LinkedIn terms note](../../README.md#a-plain-note-about-linkedins-terms) and [ADR 0008](../adr/0008-auto-send.md) first. On an active campaign, **Switch to Auto-send** on a step asks before it changes anything; **Switch to Prefill** doesn't need to.
