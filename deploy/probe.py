"""Release smoke checks; imported by deployment and exercised against real containers."""
import json
import sys
from urllib.request import urlopen
from urllib.error import URLError


def probe(base, expected_revision=None):
    def get(path):
        with urlopen(base + path, timeout=4) as response:
            return json.load(response)
    ready = get("/health/ready")
    assert ready["status"] == "ready" and ready["mode"] == "demo"
    if expected_revision:
        assert ready["revision"] == expected_revision, "Wrong release is serving traffic"
    rows = get("/api/violations")
    assert rows["mode"] == "demo" and len(rows["participants"]) == 2
    assert sorted(p["count"] for p in rows["participants"]) == [1, 2]
    assert all(p["full_name"].startswith("Example Player ") for p in rows["participants"])
    history = get("/api/audit")
    assert history["mode"] == "demo" and history["events"]
    print("Readiness, release, fictional counters and audit verified.")


if __name__ == "__main__":
    try:
        probe(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    except (AssertionError, URLError, ValueError, KeyError, OSError) as exc:
        raise SystemExit("Demo smoke check did not pass (%s)." % type(exc).__name__)
