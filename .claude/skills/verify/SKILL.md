---
name: verify
description: How to run gp-monitor's real surface locally without touching the real Georgia Power or Home Assistant accounts.
---

# Verify: gp-monitor

`gp-monitor` has two real, runnable surfaces:

## 1. CLI entrypoint (`gp-monitor poll`)

Real login/collect (against Georgia Power) always runs on `poll`, even with `--dry-run` —
`--dry-run` only skips the HA publish and metrics write. Because of that, **do not run `poll`
against the real Georgia Power servers without real, authorized credentials** — there's no safe
local fake for the upstream login.

What you *can* verify without real credentials is the CLI's failure path:

```
./venv/bin/gp-monitor poll --config <any config.yaml with a valid ha_base_url>
```

With `GP_USERNAME`/`GP_PASSWORD` unset, this fails loudly with a structured JSON log line
(`event: "auth.failed_bounded"`, `reason: "missing_credentials"`) and exit code 1 — no exception
internals or secrets leaked. That's the expected, correct behavior to confirm after any change to
`auth.py` or `cli.py`'s error handling.

## 2. `ops/preflight.py` (HA-only, safe to run for real)

This script never touches Georgia Power — only Home Assistant. Verify it against a throwaway
local stub server instead of a real HA instance:

```python
# stub_ha.py — minimal HA API stub
import http.server, sys

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/api/", "/api/states"):
            self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
        else:
            self.send_response(404); self.end_headers()
    def log_message(self, *a): pass

http.server.HTTPServer(("127.0.0.1", 8999), H).serve_forever()
```

```
python3 stub_ha.py &
```

Then, with a `config.yaml` containing `ha_base_url: http://127.0.0.1:8999` and an env file
containing `HA_LONG_LIVED_TOKEN=<anything>`:

```
./venv/bin/python ops/preflight.py --config config.yaml --env-file .env
```

Expect both checks to print `PASS` and exit 0. Point `ha_base_url` at an unreachable port
(e.g. `http://127.0.0.1:1`) to confirm the failure path: both checks print `FAIL` with a
non-leaking diagnostic message, exit 1.

Kill the stub server (`pkill -f stub_ha.py`) when done.

## What's NOT locally verifiable

Real Georgia Power login and data collection (`auth.py`'s `login()`, `collect.py`'s
`get_usage_and_billing()`) require a real Georgia Power account and are gated on Preston
supplying real credentials — see `docs/georgia-power-monitor/TASKS.md` Tasks 14–16. Don't
simulate a pass for this path locally; treat it as NOT-EXERCISED until real credentials and a
real deploy are available.
