# Running the project

## Install and pair the bot

Complete the [quick start](../README.md#quick-start-on-macos), set your group ID,
and verify `check` before installing background operation.

```sh
.venv/bin/python sportsite.py install-autostart
.venv/bin/python sportsite.py telegram-setup
```

The installation creates two user LaunchAgents:

- `university.innopolis.badminton-snapshots`
- `university.innopolis.badminton-telegram`

They start when you log in to macOS and run independently of terminal windows.
They do not wake a sleeping Mac. Keep the computer on, awake, and online.

Bot setup uses hidden token input and a one-time private pairing link. Use a
dedicated bot; setup refuses an existing webhook rather than removing it.

## Useful commands

```sh
# Show upcoming captures without contacting the website
.venv/bin/python sportsite.py schedule

# Verify website session, group, and roster access
.venv/bin/python sportsite.py check

# Create a separate manual snapshot of the next future session
.venv/bin/python sportsite.py export

# Compare a past session with the current attendance marks
.venv/bin/python sportsite.py report --date 2026-11-12

# Show local bot setup and job state; this does not test website authentication
.venv/bin/python sportsite.py telegram-status
```

To run the scheduler in a terminal rather than installing it, use `run` or
`start.command`. Avoid maintaining duplicate foreground and background workers.

## Refresh SSO

If the session expires or is revoked, open `login.command` or run:

```sh
.venv/bin/python sportsite.py login
.venv/bin/python sportsite.py check
```

Enter credentials only on the SSO website. Interactive login may require manual
VPN adjustment; optional automatic VPN handling applies to background API reads.

The alternative `login-api` command creates an optional personal InNoHassle
credential after an explicit interactive confirmation. Cookie-based operation
does not require this feature. See [security](../SECURITY.md) before enabling it.

## Data and logs

After installing autostart, the active runtime is
`~/Library/Application Support/InnoSport Badminton`:

```text
config.json            Local group, schedule, and optional VPN settings
.state/                Credentials, locks, and worker journals
exports/               Immutable scheduled and separate manual CSV/JSON captures
reports/               Attendance comparisons as text and JSON
imports/               Explicit user-provided reference lists, if any
logs/                  Scheduler, bot, and launch logs
```

All of these local data categories are excluded from the public repository.
Default notification delivery starts with captures created after notifications
were first enabled; historical snapshots are not broadcast on installation.

## Restart or update

Send `/language` in the paired private chat to choose Russian or English.
The preference is stored with the existing private Telegram settings in
`.state/telegram.json`. Preserve this file during upgrades; it also contains the
bot token, so do not share or commit it. Existing profiles default to Russian.

In Telegram, send `/violations` to view the late cancellation register and use
the **−1** buttons to reduce counters one case at a time, or 🗑 to clear a person's
current cases. People disappear automatically at zero. The list is updated by final
`/check` reports, not by a separate website poll. Existing final reports are
included when the register is opened. Deletions survive runtime updates as long
as `.state/violations.json` is retained.

Use `/undo` or the undo button to restore the last counter edit, including a full
removal. It supports the latest 20 edits recorded since the update was installed.
New violations and website corrections are preserved when undoing an edit.

The same local register file contains counters, approved screenshot cases,
dismissals, and undo history. Preserve it when moving or reinstalling the runtime.
Saved source reports live in `reports/`, and original rosters in `exports/`.
These local files are excluded from GitHub. Keeping them only on this Mac does
not provide recovery from loss of the Mac or its drive.

Restart the installed workers after editing the active configuration:

```sh
launchctl kickstart -k "gui/$(id -u)/university.innopolis.badminton-snapshots"
launchctl kickstart -k "gui/$(id -u)/university.innopolis.badminton-telegram"
```

For source changes, update the source checkout, then reinstall the runtime and
restart the bot through setup. Avoid doing this during a capture or report.

```sh
.venv/bin/python sportsite.py remove-autostart
.venv/bin/python sportsite.py install-autostart
.venv/bin/python sportsite.py telegram-setup
```

The runtime's existing exports remain. The source installation marker keeps
subsequent commands pointed at the active configuration.

`remove-autostart` removes only the snapshot LaunchAgent. To stop the separate
Telegram worker, unload its agent and remove its LaunchAgent file explicitly.
Do not delete data or credentials unless you intend to remove the installation.
