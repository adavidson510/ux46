# Email, with a quieter inbox

Email is separate from Signals and Constellation. It has its own morning brief,
full-thread reader, editable replies and Gmail filing controls. It does not add
mail contents to shared learning or room review packets.

A configured morning job prepares ten minutes before your chosen ready time.
The default target is 05:00 America/Los_Angeles. The computer must be awake and
connected; after sleep it catches up once. Email shows the actual preparation
time and a ready notice. A failed preparation preserves the previous brief and
shows the failure. Opening Email never starts a model. Prepare now is explicit.

The first brief covers up to six indexed attention-needed or recent inbox threads, using
full text where available. Coverage, account freshness and unread attachments
are visible. This is a small brief, not proof that every important message was
identified. Prepared replies require a complete bounded thread. Review the
sending account, recipients, body and uncertainties, save your edits, then Send.
Editing disables Send until the new version is saved. A changed thread blocks
a stale reply; an uncertain send is never automatically retried. Outgoing
attachments are not supported in this version; use Gmail for those messages.

Gmail labels act as folders. Filing can categorize messages, archive routine
newsletters/receipts with no attention flags, and optionally mark those read.
Messages needing a reply/decision and messages with warnings remain visible.
Mark handled in Gmail explicitly archives and marks a reviewed thread read.
No mail is deleted. Recent mailbox changes offer Undo; if the message has changed
since then, UX46 asks you to resolve it in Gmail rather than overwrite that change.
A successful send remains successful even if subsequent filing fails.

## Configure your installation

Email remains off on a fresh installation until its owner connects accounts.
Use an existing owner-only Gmail OAuth file with permissions appropriate to
read full threads, send, and modify labels. UX46 does not silently broaden those
permissions. Do not put OAuth contents into a chat or this repository.

In the private workspace directory (the directory containing email.sqlite3),
create owner-only email-assistant.json with account IDs matching the checker:

```json
{
  "accounts": {
    "personal": {
      "email": "owner@example.com",
      "token_file": "/private/mail/personal/google_token.json"
    }
  },
  "codex_command": "/path/to/codex"
}
```

The native synthesis runner uses the existing ChatGPT-authenticated Codex CLI,
an ephemeral read-only invocation with tools disabled and structured output.
There is no API-key billing fallback. Other providers can still use the workspace;
automatic email synthesis currently requires this Codex connection. Input,
time, calls and accepted output are bounded; reasoning-token cost is recorded
when provided, not claimed to have a hard cap.

Run this through your platform's user scheduler every five minutes, using your
installation's source path and private workspace path:

```sh
python tools/ux46_mail_assistant.py --directory /private/ux46/workspace --tick
```

Enable the schedule and filing policy in Email. Keep the existing metadata
checker running; it is separate from this preparation/filing job. The scheduler
performs at most one scheduled model attempt per local day; Prepare now permits
an explicit retry. Settings alone do not install an operating-system scheduler.

Provider references: [full Gmail threads](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.threads/get),
[send messages](https://developers.google.com/workspace/gmail/api/guides/sending),
[Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes).
