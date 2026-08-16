"""§18 canonical-log-line tests for gp_monitor.log — mirrors
`algo-macro-monitor/tests/test_log.py`, adapted for this repo's `SERVICE` name plus a
credential-leak regression test specific to this repo's secrets (GP_USERNAME/GP_PASSWORD/
HA_LONG_LIVED_TOKEN, per plan.md's Data model "no credential or credential-derived value is ever
logged" rule).
"""

from __future__ import annotations

import json

from gp_monitor.log import log_event, new_run_id


def _last_line(capsys) -> dict:
    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.strip()]
    assert lines, "expected at least one line on stderr"
    return json.loads(lines[-1])


def test_log_event_goes_to_stderr_not_stdout(capsys):
    log_event("info", "test.event")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() != ""


def test_log_event_has_required_canonical_fields(capsys):
    log_event("info", "poll.completed", run_id="run-1", outcome="ok", phase="config")
    line = _last_line(capsys)
    assert line["schema_version"] == 1
    assert "ts" in line and line["ts"].endswith("Z")
    assert line["level"] == "info"
    assert line["service"] == "gp-monitor"
    assert line["event"] == "poll.completed"
    assert line["msg"] == "poll.completed"  # default msg falls back to event
    assert line["run_id"] == "run-1"
    assert line["outcome"] == "ok"
    assert line["phase"] == "config"


def test_log_event_msg_override(capsys):
    log_event("warn", "auth.attempt_failed", msg="retrying after invalid login")
    line = _last_line(capsys)
    assert line["msg"] == "retrying after invalid login"


def test_log_event_is_valid_json_one_line_per_call(capsys):
    log_event("info", "a.one")
    log_event("info", "a.two")
    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.strip()]
    assert len(lines) == 2
    for line in lines:
        json.loads(line)  # must not raise


def test_log_event_native_level_spelling_not_precanonicalized(capsys):
    # §18: application code emits its own native spelling; the shared Loki pipeline
    # canonicalizes it. This module must NOT rewrite "warn" to anything else.
    log_event("warn", "collect.stale_reading")
    line = _last_line(capsys)
    assert line["level"] == "warn"


def test_log_event_redacts_denylisted_field_names(capsys):
    """Simulates the closest-thing-to-a-mistake a call site in auth.py/publish.py could make --
    passing a secret value under one of the fleet-wide deny-listed field names. Must never reach
    stderr in plaintext (defense-in-depth on top of "never pass raw exception text/response bodies
    as a field value", which is call-site discipline, not something this module can enforce)."""
    log_event(
        "error",
        "auth.failed_bounded",
        password="hunter2-gp-password",  # nosec - test fixture, not a real credential
        token="ha-long-lived-token-value",
        session="sc-web-token-value",
    )
    line = _last_line(capsys)
    assert line["password"] == "[REDACTED]"
    assert line["token"] == "[REDACTED]"
    assert line["session"] == "[REDACTED]"
    dumped = json.dumps(line)
    assert "hunter2-gp-password" not in dumped
    assert "ha-long-lived-token-value" not in dumped
    assert "sc-web-token-value" not in dumped


def test_log_event_closed_set_reason_code_never_leaks_raw_credentials(capsys):
    """Regression guard for plan.md's Data model rule: `breaker.py`/`auth.py` must only ever log a
    closed-set diagnostic reason code (e.g. 'invalid_login'), never a raw exception message that
    could echo response headers containing the JWT/Bearer token. This module has no way to enforce
    that at the call site, but this test documents and locks in the expected usage shape."""
    log_event("warn", "auth.attempt_failed", reason="invalid_login", attempt=1)
    line = _last_line(capsys)
    assert line["reason"] == "invalid_login"
    assert "Authorization" not in json.dumps(line)


def test_new_run_id_is_unique_and_stringy():
    a = new_run_id()
    b = new_run_id()
    assert isinstance(a, str) and isinstance(b, str)
    assert a != b
    assert a.startswith("run-")
