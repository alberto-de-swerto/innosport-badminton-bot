# Configuration

Copy `config.example.json` to `config.json`. The local file is ignored by Git.

| Setting | Meaning |
| --- | --- |
| `timezone` | IANA timezone used for scheduling and display |
| `title_pattern` | Case-insensitive fallback expression for the group title |
| `group_ids` | Allowed group IDs; when nonempty, takes precedence over the title expression |
| `lead_minutes` | Minutes before practice to collect the roster |
| `max_lateness_seconds` | Maximum delay for a completed scheduled response |
| `request_timeout_seconds` | Timeout for each sports-site HTTP request |
| `poll_seconds` | Scheduler polling interval; the next capture boundary can shorten it |
| `training_duration_minutes` | Fallback practice duration, 90 by default; a saved calendar end takes precedence |
| `reminder_delay_minutes` | Minutes after practice to remind the owner, 15 by default; 0–180 allowed |
| `schedule` | Weekday and start time pairs, with Monday `0` and Sunday `6` |
| `vpn_control.enabled` | Whether to pause registered macOS VPNs during sports-site requests |

## Find your group

Use an empty `group_ids` list while discovering groups:

```sh
.venv/bin/python sportsite.py login
.venv/bin/python sportsite.py list
```

Set the correct ID from the output. If several matching groups share a time,
collection fails until the configuration disambiguates them. Recheck IDs when
the semester changes. The account must be a trainer for the selected group.

## Optional macOS VPN control

The example leaves this disabled. Enable only when the website needs a direct
connection and you want the script to manage registered VPN profiles.

```json
"vpn_control": {
  "enabled": true
}
```

The controller detects active profiles through `scutil --nc`, disconnects them,
and restores only the profiles that were active before the request. Other
applications also use the direct connection during that interval.

For a provider that needs Shortcuts, create explicit **Disconnect** and
**Connect** actions for your VPN in the macOS Shortcuts app. Do not use a toggle.
Then configure your own service UUID and shortcut names:

```json
"vpn_control": {
  "enabled": true,
  "shortcut_service_id": "11111111-1111-4111-8111-111111111111",
  "disconnect_shortcut": "Badminton VPN off",
  "connect_shortcut": "Badminton VPN on"
}
```

That UUID is fictional. Use your local VPN service ID. Standalone proxies and
tunnels not listed by `scutil --nc` are outside this integration.

## Active configuration after installation

`install-autostart` copies the application and configuration to
`~/Library/Application Support/InnoSport Badminton`. Subsequent source-folder
commands follow the installation marker to that active configuration.

To explicitly choose another configuration, put `--config` before the command:

```sh
.venv/bin/python sportsite.py --config /absolute/path/config.json schedule
```

Restart the workers after changing their active configuration. See
[operations](operations.md).
