"""Tests for `gp_monitor.breaker` — the JSON circuit breaker described in plan.md's Data model
section and implemented in Step 7 of steps.md.

Covers: (a) trip after `max_consecutive_failures` and `BreakerTripped` on the next check; (b) reset
to 0 on success after a failure; (c) file created with mode 0o600; (d) no credential-shaped values
ever appear in the dumped JSON; plus atomic-write-survives-a-mid-write-crash and file-lock-prevents
a corrupting concurrent read-modify-write.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from gp_monitor.breaker import (
    SCHEMA_VERSION,
    Breaker,
    BreakerState,
    BreakerTripped,
    FailureReason,
)


@pytest.fixture
def breaker(tmp_path):
    return Breaker(path=tmp_path / "breaker_state.json", max_consecutive_failures=5)


def _perm_bits(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_trips_after_max_consecutive_failures(breaker):
    # 4 failures in a row -- not yet tripped.
    for _ in range(4):
        breaker.check_tripped()  # must not raise before the trip threshold
        breaker.record_failure(FailureReason.INVALID_LOGIN)

    state = breaker.check_tripped()
    assert state.consecutive_failures == 4

    # 5th failure reaches max_consecutive_failures=5.
    breaker.record_failure(FailureReason.INVALID_LOGIN)

    with pytest.raises(BreakerTripped) as exc_info:
        breaker.check_tripped()
    assert exc_info.value.consecutive_failures == 5
    assert exc_info.value.last_failure_reason == "invalid_login"
    # `last_failure_at` is threaded through from the on-disk state, not dropped/hardcoded to None.
    assert exc_info.value.last_failure_at is not None
    message = str(exc_info.value)
    assert "5 consecutive failures" in message
    assert "invalid_login" in message
    assert exc_info.value.last_failure_at in message


def test_record_failure_accepts_string_matching_closed_set(breaker):
    state = breaker.record_failure("rate_limited")
    assert state.last_failure_reason == "rate_limited"


def test_record_failure_rejects_reason_outside_closed_set(breaker):
    with pytest.raises(ValueError):
        breaker.record_failure("some raw exception text with a token=abc123")


def test_reset_after_success(breaker):
    breaker.record_failure(FailureReason.NO_JWT)
    state = breaker.check_tripped()
    assert state.consecutive_failures == 1
    assert state.last_failure_reason == "no_jwt"
    assert state.last_failure_at is not None

    reset_state = breaker.record_success()
    assert reset_state.consecutive_failures == 0
    assert reset_state.last_failure_reason is None
    assert reset_state.last_failure_at is None

    # Persisted state agrees -- last_failure_at/reason cleared together with the counter, not left
    # stale next to a 0 counter.
    on_disk = json.loads(breaker.path.read_text())
    assert on_disk["consecutive_failures"] == 0
    assert on_disk["last_failure_at"] is None
    assert on_disk["last_failure_reason"] is None
    assert on_disk["schema_version"] == SCHEMA_VERSION


def test_file_created_with_0600_perms(breaker):
    breaker.record_failure(FailureReason.CANT_REACH_GP)
    assert breaker.path.exists()
    assert _perm_bits(breaker.path) == 0o600


def test_file_stays_0600_after_success_reset(breaker):
    breaker.record_failure(FailureReason.PUBLISH_FAILED)
    breaker.record_success()
    assert _perm_bits(breaker.path) == 0o600


def test_no_credentials_in_dumped_json(breaker):
    breaker.record_failure(FailureReason.INVALID_LOGIN)
    breaker.record_success()
    breaker.record_failure(FailureReason.CANT_REACH_GP)

    raw_text = breaker.path.read_text()
    on_disk = json.loads(raw_text)

    # Only the closed-set schema fields are present.
    assert set(on_disk.keys()) == {
        "schema_version",
        "consecutive_failures",
        "last_failure_at",
        "last_failure_reason",
    }
    # The reason is one of the closed-set enum values, never free text.
    assert on_disk["last_failure_reason"] in {r.value for r in FailureReason}

    credential_shaped_markers = ("password", "token", "jwt", "secret", "authorization", "bearer")
    lowered = raw_text.lower()
    for marker in credential_shaped_markers:
        assert marker not in lowered, f"found credential-shaped marker {marker!r} in breaker file"


def test_atomic_write_survives_mid_write_crash(breaker, monkeypatch):
    """A crash after the good state is on disk but during a *subsequent* write must never corrupt
    the previously-persisted, complete file -- only ever a fully-written temp file or nothing."""
    breaker.record_failure(FailureReason.COLLECT_FAILED)
    good_contents = breaker.path.read_text()

    import gp_monitor.breaker as breaker_module

    def boom(*args, **kwargs):
        raise OSError("simulated crash mid-write")

    monkeypatch.setattr(breaker_module.os, "replace", boom)

    with pytest.raises(OSError):
        breaker.record_failure(FailureReason.PUBLISH_FAILED)

    # Original file untouched -- still valid JSON, still the pre-crash state.
    assert breaker.path.read_text() == good_contents
    state = BreakerState.from_dict(json.loads(good_contents))
    assert state.consecutive_failures == 1
    assert state.last_failure_reason == "collect_failed"

    # No leftover temp file in the directory.
    leftover_tmp = [p for p in breaker.path.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftover_tmp == []


def test_atomic_write_json_original_crash_propagates_even_if_cleanup_unlink_also_fails(
    breaker, monkeypatch
):
    """Pins `contextlib.suppress(OSError)` around the cleanup `os.unlink` -- if the temp file's own
    cleanup also fails (e.g. another process already removed it), that must not mask/replace the
    *original* crash's exception. Only observable by making both `os.replace` and the cleanup
    `os.unlink` fail in the same write."""
    breaker.record_failure(FailureReason.COLLECT_FAILED)

    import gp_monitor.breaker as breaker_module

    def boom_replace(*args, **kwargs):
        raise OSError("simulated crash mid-write")

    def boom_unlink(*args, **kwargs):
        raise OSError("cleanup also failed")

    monkeypatch.setattr(breaker_module.os, "replace", boom_replace)
    monkeypatch.setattr(breaker_module.os, "unlink", boom_unlink)

    with pytest.raises(OSError, match="simulated crash mid-write"):
        breaker.record_failure(FailureReason.PUBLISH_FAILED)


def test_lock_serializes_sequential_read_modify_write_cycles(breaker):
    """Simulates two "instances" (a timer-triggered run and a manually-triggered run) racing to
    record a failure. Both use the same lock file, so sequential calls -- standing in for what the
    flock forces two real concurrent processes to do -- must never lose an increment."""
    other = Breaker(path=breaker.path, max_consecutive_failures=breaker.max_consecutive_failures)

    breaker.record_failure(FailureReason.RATE_LIMITED)
    other.record_failure(FailureReason.RATE_LIMITED)
    breaker.record_failure(FailureReason.RATE_LIMITED)

    final_state = BreakerState.from_dict(json.loads(breaker.path.read_text()))
    assert final_state.consecutive_failures == 3


def test_lock_file_is_separate_from_data_file(breaker):
    breaker.record_failure(FailureReason.NO_TOKEN)
    assert breaker.lock_path.exists()
    assert breaker.lock_path != breaker.path
    assert breaker.lock_path.name == "breaker_state.json.lock"


def test_missing_file_treated_as_untripped_zero_state(breaker):
    assert not breaker.path.exists()
    state = breaker.check_tripped()
    assert state == BreakerState()


def test_atomic_write_json_creates_missing_nested_parent_directories(tmp_path):
    """Pins `mkdir(parents=True, ...)` directly -- `Breaker.record_failure`'s own `_locked()` call
    creates the parent dir first (also with `parents=True`), which would mask this mutation if
    tested only through `record_failure`/`record_success`. Calling `_atomic_write_json` directly
    against a path with *two* missing levels isolates it."""
    import gp_monitor.breaker as breaker_module

    nested_path = tmp_path / "a" / "b" / "breaker_state.json"
    breaker_module._atomic_write_json(nested_path, {"schema_version": 1})
    assert nested_path.exists()


def test_breaker_default_max_consecutive_failures_is_five(tmp_path):
    """Pins the `Breaker.__init__` default of `max_consecutive_failures=5` directly -- the other
    tests all pass it explicitly, so a mutant changing the default alone would otherwise survive."""
    default_breaker = Breaker(path=tmp_path / "breaker_state.json")
    assert default_breaker.max_consecutive_failures == 5


def test_now_iso_produces_a_timezone_aware_utc_timestamp(breaker):
    """Pins `datetime.now(UTC)` against a mutant that swaps in `datetime.now(None)` (naive local
    time) -- the recorded `last_failure_at` must carry UTC offset info."""
    import datetime as dt

    state = breaker.record_failure(FailureReason.NO_JWT)
    parsed = dt.datetime.fromisoformat(state.last_failure_at)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == dt.timedelta(0)


def test_check_tripped_boundary_not_tripped_one_below_max(breaker):
    """Pins `>=` exactly: at `consecutive_failures == max_consecutive_failures - 1`, the breaker
    must not be tripped yet."""
    for _ in range(4):
        breaker.record_failure(FailureReason.INVALID_LOGIN)
    state = breaker.check_tripped()  # must not raise
    assert state.consecutive_failures == 4


def test_atomic_write_json_sorts_keys_alphabetically(breaker):
    """Pins `sort_keys=True` -- `BreakerState`'s dataclass field declaration order
    (schema_version, consecutive_failures, last_failure_at, last_failure_reason) differs from
    alphabetical order, so this is only satisfied by an actual sort, not dict insertion order."""
    breaker.record_failure(FailureReason.NO_TOKEN)
    raw_text = breaker.path.read_text()
    keys_in_order = [line.split('"')[1] for line in raw_text.splitlines() if line.strip().startswith('"')]
    assert keys_in_order == sorted(keys_in_order)
    assert keys_in_order[0] == "consecutive_failures"  # alphabetically first of the four


def test_atomic_write_json_produces_pretty_printed_indented_json(breaker):
    """Pins `indent=2` -- a flat/compact JSON dump would still round-trip through `json.loads`
    (the other existing tests only check parsed content), so this checks the raw on-disk text
    directly for indentation."""
    breaker.record_failure(FailureReason.RATE_LIMITED)
    raw_text = breaker.path.read_text()
    # Exactly 2-space indent -- not 1, not 3 -- on the first nested line.
    first_nested_line = raw_text.splitlines()[1]
    indent_width = len(first_nested_line) - len(first_nested_line.lstrip(" "))
    assert indent_width == 2
    assert raw_text.endswith("\n")  # trailing newline written explicitly


def test_atomic_write_json_writes_via_tempfile_with_tmp_suffix(breaker, monkeypatch):
    """Pins `tempfile.mkstemp(..., suffix=".tmp", ...)` directly via a spy -- the on-disk leftover
    checks in the crash test can't distinguish a `suffix=".tmp"` mutant because cleanup removes the
    temp file regardless of its name."""
    import gp_monitor.breaker as breaker_module

    recorded_kwargs = {}
    original_mkstemp = breaker_module.tempfile.mkstemp

    def spy_mkstemp(*args, **kwargs):
        recorded_kwargs.update(kwargs)
        return original_mkstemp(*args, **kwargs)

    monkeypatch.setattr(breaker_module.tempfile, "mkstemp", spy_mkstemp)
    breaker.record_failure(FailureReason.NO_TOKEN)

    assert recorded_kwargs["suffix"] == ".tmp"
    assert recorded_kwargs["prefix"] == f".{breaker.path.name}."
    # `dir=` must be the target file's own parent directory -- not the system default temp dir --
    # so `os.replace()` stays a same-filesystem, atomic rename.
    assert recorded_kwargs["dir"] == str(breaker.path.parent)


def test_atomic_write_json_chmods_temp_file_to_0600_before_replace(breaker, monkeypatch):
    """Pins the intermediate `os.chmod(tmp_name, 0o600)` call directly -- the final on-disk path's
    permissions are re-set again by the caller (`record_failure`/`record_success`), so a mutant
    changing the mode passed to this *intermediate* chmod is otherwise unobservable from the final
    file's permission bits alone."""
    import gp_monitor.breaker as breaker_module

    recorded_calls = []
    original_chmod = breaker_module.os.chmod

    def spy_chmod(path, mode):
        recorded_calls.append((str(path), mode))
        return original_chmod(path, mode)

    monkeypatch.setattr(breaker_module.os, "chmod", spy_chmod)
    breaker.record_failure(FailureReason.NO_TOKEN)

    # First chmod call is on the temp file (before the atomic rename), with mode 0o600.
    tmp_calls = [c for c in recorded_calls if c[0] != str(breaker.path)]
    assert tmp_calls, "expected a chmod call on the temp file distinct from the final path"
    assert tmp_calls[0][1] == 0o600


def test_breaker_state_round_trips_through_from_dict():
    state = BreakerState(
        schema_version=1,
        consecutive_failures=3,
        last_failure_at="2026-08-16T00:00:00+00:00",
        last_failure_reason="publish_partial",
    )
    assert BreakerState.from_dict(state.to_dict()) == state
