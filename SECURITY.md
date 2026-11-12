# Security and privacy

## Publication boundary

This repository contains code, an example configuration, and fictional test data.
It must not contain real student rosters, screenshots, reports, session cookies,
Telegram tokens, personal API keys, local VPN profile IDs, or machine-specific paths.

`config.json`, `.state/`, `exports/`, `reports/`, `imports/`, `outputs/`, `logs/`,
and environment files are excluded by `.gitignore`. Never force-add these paths.
Examples and issues should use fictional data.

## Authentication

- University passwords are entered in the SSO browser and are not stored by the client.
- Session persistence keeps the sports website's `sessionid`, not the SSO provider's cookies.
- Telegram setup uses hidden local token input and an unpredictable pairing link. The listener accepts commands only from the paired user in that private chat.
- Existing webhooks are not removed automatically, and the listener uses a local lock to prevent duplicate consumers.
- Optional InNoHassle personal credentials are kept locally. They authorize access under the owner's account; the client's read-only behavior does not reduce the permissions granted to the credential by the service.

The client creates sensitive directories with mode `700` and data/credential files
with mode `600`. These permissions do **not** isolate secrets from other programs
running as the same OS user, or from administrators.

## Data handling

Snapshots retain names and student IDs. Comparison reports also retain credited
hours. Email addresses and medical information returned by the website are dropped.
The late cancellation register keeps names, student IDs, session dates, and
comparison timestamps. Dismissal identifiers prevent a repeated comparison from
restoring hidden cases. Current source cases and the latest 20 reversible edits
retain names and case details so undo can restore them without overwriting newer
website corrections; source reports and snapshots remain unchanged. Register
pages, counter edits, and undo are available only to the paired owner in their
private Telegram chat.
Explicitly confirmed screenshot cases also retain their import source and the
confirmation time. They are not presented as automatic snapshots. Decreasing a
counter forgives one case and uses the same durable dismissal mechanism.
Reminder and alert journals store event identifiers, statuses, and timestamps.
Automatic problem messages use fixed descriptions rather than forwarding raw
error payloads. No credentials or medical details are included in these alerts.
Telegram receives names, credited hours where relevant, and session information.
Use the tool only with appropriate account access and inform participants about
the club's handling of attendance data.

Request errors are sanitized because raw HTTP client errors can include cookies,
authorization headers, or Telegram token-bearing URLs. Authentication and schema
failures do not become empty successful exports.

## VPN behavior

VPN control is disabled in the example configuration. If enabled, it can change
connectivity for **all** applications on the computer during a website request.
Only previously active registered VPN profiles are restored. A durable journal
allows the worker to retry restoration after an interruption; abrupt power loss
or an unavailable profile can still require manual recovery.

## Reporting a problem

Do not put tokens, cookies, real names, screenshots of student lists, or complete
private logs in public issues. For a security-sensitive issue, contact the
repository owner privately or use GitHub's private vulnerability reporting when
available. Share a minimal, redacted reproduction.
