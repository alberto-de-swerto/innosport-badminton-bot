# Contributing

Use Python 3.9-compatible syntax. Python 3.11+ is recommended for development.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

Tests read `config.example.json` and create temporary projects. They do not need a
local `config.json`, real authentication, or a Chromium download.

When changing collection or reporting:

- Keep sports-site operations read-only.
- Preserve the distinction between scheduled captures, manual captures, and explicitly imported reference lists.
- Retain the actual collection time. Never backdate a late or missed capture.
- Validate group, session, and participant IDs before comparing records.
- Cover meaningful failure paths with fictional data, mocked commands, or the local HTTP server.
- Do not log credential-bearing request errors or add personal data to fixtures.

Run the relevant tests before submitting a pull request. Explain the behavior
change, why it is needed, and how it was verified. See [SECURITY.md](SECURITY.md)
before attaching logs or examples.
