# InnoSport Badminton Bot

[![Tests](https://github.com/alberto-de-swerto/innosport-badminton-bot/actions/workflows/tests.yml/badge.svg)](https://github.com/alberto-de-swerto/innosport-badminton-bot/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A Python side project for running a university badminton club: save registrations one hour before practice, send the roster to Telegram, and compare it with attendance afterwards.

Built around a real coordination problem at [Innopolis University](https://sport.innopolis.university/). Students sometimes cancel immediately before a full session starts, leaving others with no chance to take the place. A saved roster gives the club leader a reference for reviewing what changed.

## What it does

- **Scheduled snapshots** of Beginners registrations, saved locally as CSV and JSON with the actual capture time.
- **Automatic Telegram notifications** after a successful scheduled export, including every registered participant's name.
- **Attendance comparisons** through `/check`, after the leader saves attendance hours on the website.
- **Late cancellation register** with per-person counts, session dates, and removal buttons in Telegram.
- **Undo for register edits**, post-practice reminders, and deduplicated export problem alerts.
- **Interactive SSO sign-in** with a reusable local session. Passwords are entered only on the university login page.
- **Independent background workers** for exports and Telegram, installed as macOS LaunchAgents.
- **Optional VPN handling** through macOS network profiles or Shortcuts, with restoration after requests and recovery after interruption.
- **Persistent state and atomic writes** for retries, restart recovery, and distinguishing missed jobs from successful snapshots.
- **Optional PostgreSQL register**, verified JSON migration, transactional counter edits and actor-attributed audit history.
- **Fictional portfolio demo** with a container image, blue/green releases, smoke checks and tested rollback.

This is an independent club tool, not an official university service. The website account must already have permission to view the group's participants. Bot messages support Russian and English.

## Example

The following notification uses fictional participants:

```text
✅ Список за час до тренировки успешно сохранён
🏸 Beginners · 12.11.2026 20:00 МСК
Выгрузка: 12.11 19:00:05 МСК
Планируют прийти: 3

1. Example Player One
2. Example Player Two
3. Example Player Three

После занятия сохрани часы на сайте и отправь /check.
```

After attendance is marked, `/check` separates the original roster, participants who disappeared without receiving hours, attendees with credited hours, original registrations with zero hours, and later additions. See the [sample comparison](docs/example-report.md).

## How it works

```mermaid
flowchart LR
    Website[Sports website] --> Exporter[Snapshot worker]
    Exporter --> Files[Local CSV and JSON]
    Files --> Bot[Telegram worker]
    Bot --> Chat[Private Telegram chat]
    Chat -->|/check| Bot
    Files --> Comparison[Attendance comparison]
    Website --> Comparison
    Comparison --> Bot
```

The browser is needed for interactive SSO sign-in. Scheduled exports use Playwright's HTTP request context and the saved sports-site session. The client only reads sports data: it does not register students, cancel places, or assign hours.

The Telegram worker watches completed scheduled exports and sends their saved contents. Telegram outages do not block collection. Reports compare students by stable ID rather than display name.

Read more about [the architecture and reliability decisions](docs/architecture.md).

## Quick start on macOS

Requirements: Python 3.9+ (3.11+ recommended), a sports-site account with trainer access, and a Telegram bot created through [@BotFather](https://t.me/BotFather).

```sh
git clone https://github.com/alberto-de-swerto/innosport-badminton-bot.git
cd innosport-badminton-bot
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
cp config.example.json config.json
```

Edit `config.json` for your group and timetable. VPN control is disabled in the example. Find the right group and verify access:

```sh
.venv/bin/python sportsite.py login
.venv/bin/python sportsite.py list
```

Sign in through SSO in the opened browser. Put the appropriate group ID in `group_ids`, then run:

```sh
.venv/bin/python sportsite.py check
.venv/bin/python sportsite.py schedule
.venv/bin/python sportsite.py install-autostart
.venv/bin/python sportsite.py telegram-setup
```

Telegram setup asks for the bot token with hidden input and gives you a one-time pairing link. Open it in your Telegram account and press **Start**. Only that private chat can use the bot.

Alternatively, double-click `setup.command`, then `login.command`. Configure the group before installing autostart; open `telegram-setup.command` after installation.

Both workers run independently of the terminal or coding tools. **The Mac must stay on, awake, and online.** The LaunchAgents start when you log in to macOS. The SSO session can expire and require a new interactive login.

See [configuration](docs/configuration.md) and [day-to-day operation](docs/operations.md).

## Default timetable

The example uses `Europe/Moscow` and a 60-minute lead time. All entries are configurable.

| Day | Practice | Snapshot |
| --- | --- | --- |
| Thursday | 20:00 | 19:00 |
| Saturday | 18:30 | 17:30 |
| Sunday | 18:00 | 17:00 |

## Telegram commands

| Command | Result |
| --- | --- |
| `/check` | Compare the most recent session that has started |
| `/check YYYY-MM-DD` | Compare a specific session |
| `/violations` | Show possible late cancellations, counters, dates, −1 and removal buttons |
| `/undo` | Undo the latest counter decrease or full removal |
| `/status` | Show recent snapshot jobs and the next scheduled capture |
| `/language` | Choose Russian or English for bot messages |
| `/history` | Show the last 10 register changes, actors and count deltas (PostgreSQL) |
| `/help` | Show usage instructions |

Save attendance hours on the sports website before calling `/check`. Automatic roster notifications require no command.

Use `/language` and tap **Русский** or **English**, or send `/language ru` or
`/language en` directly. The choice survives restarts and applies to command
descriptions, reports, register controls, reminders, and problem alerts. Russian
is the default for existing installations. Participant names stay as supplied by
the website. Saved reports and CLI output remain in Russian; changing the bot's
language does not change counters or resend old notifications.

After a completed session, `/check` updates a private local register of people who
were in the scheduled snapshot but disappeared without receiving hours. Open
`/violations` to see names, counts, and dates; use the arrow buttons to change pages.
Existing saved final reports are included automatically. Preliminary comparisons
and historical tests based on screenshots are excluded from automatic counting.
An individual screenshot case can be explicitly confirmed by the owner; its date
then carries a `*` marker so its source stays visible.

Each person counts once per session. A later `/check` replaces that session's
result, so correcting attendance removes an outdated case. Use **−1** beside a
person to forgive their oldest case and reduce their counter by one. At zero the
person disappears from the list automatically. The 🗑 button clears all currently
listed cases for that person. Rechecking or restarting will not restore forgiven
cases, including manually confirmed screenshot cases; a cancellation at a new
session can add the person again. An old button cannot reduce a counter that has
changed since it was displayed: the bot refreshes the list and asks you to use the
updated button.

Use `/undo` or **↩️ Отменить последнее действие** to reverse a counter decrease or
full removal, even when the list became empty or the bot restarted. The last 20
edits made after this feature is installed can be undone, newest first. Undo
restores individual cases, preserving new cases and newer website corrections.
Retrying delivery of the same command does not undo an extra edit.

The default register is stored in `.state/violations.json` with a file lock and atomic
writes. PostgreSQL is enabled through an explicit migration; it stores cases,
dismissals, undo history and an audit trail in one transaction. See
[PostgreSQL setup and migration](docs/postgresql.md). Private data is excluded
from Git. Removing a register entry does not change the
original snapshots, comparison reports, website registrations, or attendance hours.

## Reminders and problem alerts

The bot reminds the owner to save attendance hours and send `/check YYYY-MM-DD`
15 minutes after a session ends. A saved final comparison suppresses that
session's reminder; preliminary comparisons do not. The end time comes from the
saved calendar event, with `training_duration_minutes` (90 by default) as a
fallback. `reminder_delay_minutes` controls the delay.

With the example timetable and 90-minute sessions, reminders are Thursday 21:45,
Saturday 20:15, and Sunday 19:45, all in `Europe/Moscow`. Missed reminders expire
after three hours; installation does not broadcast old reminders.

The independent Telegram worker also watches capture jobs and startup status. It
alerts about authentication, permission, data validation, and collection errors;
missed captures; unreadable journals; and invalid saved snapshots. A missing job
can be detected after its deadline even if the exporter did not start. Alerts
use fixed safe descriptions, never raw credential-bearing error details.

Delivery checkpoints are stored in `.state/telegram-service-notifications.json`.
Repeated retries and restarts do not normally repeat the same alert or reminder.
These features require the Mac and Telegram worker to be running; internet
outages delay delivery, and a lost acknowledgement can still cause a duplicate.
They do not make extra sports-site requests or mark attendance automatically.

## Engineering choices

| Concern | Approach |
| --- | --- |
| A missed deadline must not look like a historical capture | Explicit `saved`, `retry`, and `missed` states; actual request and capture timestamps |
| A process can stop during a write | Write to a temporary location and atomically publish the completed snapshot |
| Workers can overlap | File locks around scheduling and VPN operations |
| Telegram can be unreachable | Separate notification worker with a durable per-message delivery checkpoint |
| Names can change or repeat | Compare student IDs |
| Website data can be inconsistent | Validate group, session time, schema, and pre-session registration count |
| Credentials and rosters are private | Local files with restricted permissions, excluded from Git |

## Testing

```sh
.venv/bin/python -m unittest discover -s tests -v
```

The tests use fictional people, mocked VPN commands, and a local HTTP server. They cover scheduling, SSO/session failures, token transport, atomic exports, missed deadlines, report categories, Telegram pairing and authorization, notification retries, restart recovery, register corrections, durable dismissals, callback authorization, stale buttons, and concurrent register writes. They do not need real credentials, a Telegram account, or a browser download.

GitHub Actions runs the suite on Linux with Python 3.11 and 3.13. Native macOS integration is tested separately; CI does not validate your VPN, SSO account, or local LaunchAgents.

An additional CI job runs real PostgreSQL integration tests. Container tests promote
two demo revisions and exercise rollback before and after traffic switches. Successful
`main` builds publish the exact tested demo image to GHCR. SSH deployment is enabled
only after a demo server is configured; no server is assumed or provisioned.
See [demo releases and deployment](docs/demo-deployment.md).

## Limitations

- The website exposes current registrations and attendance marks, not a complete cancellation history. Cancel-and-rejoin events and cancellations by people with positive hours cannot be identified from this table. The client also cannot tell who removed a registration.
- Zero hours means no attendance credit was saved. It is not independent proof that someone was physically absent.
- The default capture window allows up to 120 seconds of network delay and records the actual time. This is not a transactionally consistent database snapshot.
- A manual export does not replace a missed scheduled snapshot. Explicitly imported reference lists are labelled as user-provided.
- Telegram delivery is retried. A lost acknowledgement after Telegram accepts a message can result in a duplicate.
- The integration depends on the university website's API and permission model. Group IDs may change between semesters.

## Project layout

```text
sportsite.py              API access, SSO, exports, scheduler, and CLI
telegram_bot.py           Private bot, commands, and roster notifications
attendance_report.py      Baseline validation and attendance comparison
violation_registry.py     Local late cancellation counts and durable dismissals
registry_database.py      PostgreSQL tables, transactions and audit events
registry_admin.py         Verified JSON migration and private administration
migrations/              Versioned Alembic schema migrations
demo_app.py               Read-only fictional demo and readiness endpoints
deploy/                   Compose stack, release smoke checks and configuration
scripts/                  Blue/green deployment and real rollback regression
bot_notifications.py      Post-practice reminders and export problem alerts
i18n.py                   Russian/English bot messages and language helpers
vpn_control.py            Optional macOS VPN pause and recovery
api_auth.py               Optional personal InNoHassle token support
config.example.json       Shareable configuration template
tests/                    Unit and local HTTP integration tests
docs/                     Setup details and fictional examples
```

See [SECURITY.md](SECURITY.md) for credential handling and publication boundaries, and [CONTRIBUTING.md](CONTRIBUTING.md) for development notes.

## Acknowledgements and license

The integration was developed by studying the public [InnoSport source](https://github.com/one-zero-eight/sport). It uses [Playwright](https://playwright.dev/python/docs/auth) and the [Telegram Bot API](https://core.telegram.org/bots/api). Those projects and services retain their own licenses and terms.

This project's code is available under the [MIT License](LICENSE).
