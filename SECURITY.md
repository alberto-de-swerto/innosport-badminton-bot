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
- Optional InNoHassle personal credentials are kept locally. They authorize access under the owner's account; client-side restrictions do not reduce the permissions granted to the credential by the service.

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

PostgreSQL connection settings are private. Database audit rows contain names,
actor IDs, reasons and case identifiers, and remain after counters are cleared.
They are append-only through the application, not tamper-proof against database
administrators. The original JSON is retained during migration; protect it and
database backups equally. A database failure never silently selects stale JSON.

The public demo uses a separate, explicitly marked fictional database and refuses
a live database. The live backend also refuses a demo database. The image build
uses a source allowlist. Only the read-only demo is published; real credentials,
rosters and Telegram state are not shipped. Demo and bot databases must remain
separate. SSH deployment requires a preverified host key; keys are not discovered
and trusted automatically during the workflow.

## Attendance editing

Only the paired private-chat owner can issue attendance commands or press hour
buttons. Persisted opaque action tokens bind owner, session, student, expected
hours and an absolute target value. The worker rechecks trainer/group permissions,
the site editing flag, session identity, duration and the start-to-72-hours window.
One POST can set one student's attendance hours. The integration contains no
booking or enrollment call. Search shows at most five server-filtered matches;
medical information in legacy suggestion values is discarded.

Session-based writes use the site's normal CSRF cookie/header and same-origin
Referer. The CSRF token exists only in the request context's memory. Bearer-based
authentication is supported without impersonating a browser login. Search emails
and names are used in the private editor UI and its protected action journal,
never copied into public fixtures or ordinary snapshot/report exports.

`.state/attendance-actions.json` is private and stores in-flight/completed edits
for replay protection and operational audit. An uncertain response is resolved by
reading saved hours; it is never blindly retried. A failed local reconciliation
does not reverse or disguise a confirmed website write. `/undo` affects violation
counters only, not attendance hours on the university site.

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
