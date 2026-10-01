# Check usage without leaving the conversation

Type `/usage` in the composer. It opens immediately, including while an agent
is working. It does not send a model prompt, restart a connection, redeem a
reset, or change billing settings.

The account section shows each allowance window reported by the native
runtime, its remaining percentage and local reset time, plus whether additional
credits and unused allowance resets are reported. An exhausted included
allowance does not prove that every request is blocked: credit-backed access or
a different limit bucket can be separate. This view does not establish which
funding source paid for a particular response.

Conversation token receipts appear separately, for the displayed UTC date.
Unknown or unsupported fields remain unavailable rather than appearing as zero.
A historical failed turn is labelled as an earlier attempt. Account allowance
alone does not turn a successful answer into a failed turn.

## Connector contract

Authenticated `GET /api/account-usage` reads native `account/rateLimits/read`.
The response contains sanitized allowance windows and credit availability,
never credentials, raw account IDs, credit balances or redemption IDs. Other
connectors may leave this unsupported; the UI keeps their token receipts useful.

For an older running console, the optional gateway `--account-usage-config`
accepts an operator-owned JSON object with `local_agent` and `agents`, mapping
fixed agent IDs to command argument arrays. Each command runs the bundled
`tools/ux46_account_usage.py --codex /absolute/path/to/codex` on that agent's
host and normal OS account. SSH commands must use the existing scoped agent
transport. The browser cannot choose a command, host, executable or login.

The probe opens only a short-lived native metadata connection and closes it;
it never opens or resumes a thread. Results are bounded to 18 seconds and
cached for 15 seconds per configured agent. This source is labelled as the
currently saved login, which can differ from an older connected worker.
No background polling is added. Existing conversations stay connected.
