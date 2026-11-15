# Set attendance hours in Telegram

Use `/attendance`, choose a session, then select a participant and their hours.
The hour button saves immediately to the sports website and verifies the result
by reading it back. **+1** is shown with its resulting value; repeated delivery of
the same button never adds another hour. Absolute buttons include 0 through the
session's maximum academic hours (normally 2 for a 90-minute practice).

You can also open a particular date with `/attendance YYYY-MM-DD`. The bot only
offers configured sessions which have already started and are no more than
72 hours from their start. These checks run again before writing; an old button
cannot bypass the deadline. The sports website's own permissions and editing
window also apply. Future training bookings are never performed.

## Find someone who is not in the table

Press **🔎 Find by email** and type at least three characters, such as `al.muk`.
The bot shows up to five matching names/emails. Select the right person, check
their identity and session date, then choose hours. Setting hours creates an
attendance mark for that session; it does not occupy a registration place.

Alternatively use `/find partial_email [YYYY-MM-DD]`. Without a date it searches
for the most recent eligible session and shows that session in the result header.
Names are searchable as well. `/cancel` leaves contextual search. Results respect
the sports website's filtering for the selected group. There is no automatic
selection when several students match.

## Saved hours and violations

After a confirmed save for a completed session, the worker runs a fresh attendance
comparison. A credited participant's earlier no-show/cancellation is removed,
while manual violation dismissals remain preserved. Zero credited hours for an
original registrant count as a no-show. Finish marking attendance before treating
the violation list as final; intermediate zero-hour rows can still be unmarked
attendees. During a running session, the bot asks for a final `/check` after it ends.

If hours were saved but the comparison could not finish, the bot says so and asks
for `/check YYYY-MM-DD`. It does not send the attendance write again. Missing
snapshots cannot be reconstructed by setting hours. `/undo` reverses violation
counter edits only. To correct hours, reopen the participant and select the right
absolute value within the editing window.

## Retries and safeguards

Buttons expire after 15 minutes. Each is bound to the paired owner, date, group,
student and expected previous hours. When hours changed on the website since the
button was displayed, the bot asks to reopen that participant. The upstream API
does not provide an atomic conditional update; avoid simultaneous edits from
another website client while using the bot.

If a response is lost after a save, the worker reads the current value to confirm
it. A persisted in-flight/uncertain operation is never blindly posted again. If
confirmation remains unavailable, check the site and refresh `/attendance`.
The private journal `.state/attendance-actions.json` retains recent actions and
in-flight operations; completed records are bounded to roughly 2,000 UI actions.
It may contain selected students' names and search emails. Protect it alongside
other private runtime files. Snapshot and comparison exports still omit emails
and medical data.

Automated tests use fictional students, a local HTTP server and mocked Telegram.
They verify JSON/CSRF transport, owner/expiry checks, the 72-hour boundary,
partial email parsing, duplicate/uncertain writes, permission changes and
reconciliation with JSON and PostgreSQL registers. A real-site smoke check reads
the roster, search results and CSRF availability without assigning test hours.
