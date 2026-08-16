"""Local JSON circuit breaker for bounded retry state across scheduled polls.

Mirrors the shape of internal-monitor-app's Poshmark login circuit breaker
(`poshmark/login-circuit-breaker.ts`) — a plain JSON file, not a database, is enough for one
counter (plan.md's Data model section) — adapted to Python for this repo rather than reused via
`scraper-commons`' `admission` module (see plan.md's Technology choices).

State lives at `data/breaker_state.json` (gitignored, mode 0600):

```
{
  "schema_version": 1,
  "consecutive_failures": <int>,
  "last_failure_at": "<ISO8601>",
  "last_failure_reason": "<FailureReason value>"
}
```

Hardening requirements from plan.md / the adversarial spec review, implemented exactly here (not
optional):

- `last_failure_reason` is enforced via a real `FailureReason` `Enum` at write time, not just
  documented as a closed set — `record_failure` raises `ValueError` on anything else.
- Writes are atomic: build the new JSON in a temp file in the *same* directory as the target, then
  `os.replace()` (an atomic rename on POSIX) over the real path. A process killed mid-write can
  never leave a corrupt/partial `breaker_state.json` — a reader either sees the old complete file
  or the new complete file, never a torn one.
- `schema_version: 1` is included from the start for future migration safety.
- A file lock (`fcntl.flock` on a dedicated `.lock` file, held for the whole read-modify-write
  cycle) prevents a manually-triggered `systemctl start gp-monitor-poll.service` from racing a
  timer-triggered run and corrupting the breaker state. The lock is a separate file from the data
  file on purpose — flock'ing the data file itself would race the atomic rename (the lock is tied
  to an inode; `os.replace` swaps the directory entry to a different inode out from under it).
- `record_success` clears `last_failure_at`/`last_failure_reason` together with resetting
  `consecutive_failures` to 0 — never leaves a stale failure reason sitting next to a 0 counter.
- The file is created/rewritten with mode 0600 and never contains a credential or
  credential-derived value (JWT, password, token, session cookie, raw exception text, HTTP
  response body) — only a closed-set diagnostic code and an ISO8601 timestamp. Callers must not
  pass raw exception text as `reason`; `FailureReason` makes that structurally impossible.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

SCHEMA_VERSION = 1

# Relative to the process's working directory — same convention as `config.py`'s
# `DEFAULT_CONFIG_PATH`. The systemd unit sets `WorkingDirectory=` to the repo's deploy root, so
# this resolves to that root's `data/` directory in production.
DEFAULT_BREAKER_PATH = "data/breaker_state.json"


class FailureReason(StrEnum):
    """Closed set of diagnostic codes — see plan.md's Data model section and cli.py's Step 11b
    guard-check mapping. Deliberately never a raw exception message or HTTP response body (either
    could echo back the Georgia Power JWT/session token or the HA Bearer token)."""

    CEASE_HALTED = "cease_halted"
    INVALID_LOGIN = "invalid_login"
    NO_JWT = "no_jwt"
    NO_TOKEN = "no_token"
    CANT_REACH_GP = "cant_reach_gp"
    COLLECT_FAILED = "collect_failed"
    PUBLISH_FAILED = "publish_failed"
    PUBLISH_PARTIAL = "publish_partial"
    RATE_LIMITED = "rate_limited"


class BreakerTripped(Exception):
    """Raised by `Breaker.check_tripped()` once `consecutive_failures` has reached the configured
    `max_consecutive_failures`. The cycle must abort before attempting login — see plan.md's Data
    model section ("the cycle logs and exits without attempting login at all, until a human clears
    the file")."""

    def __init__(
        self,
        consecutive_failures: int,
        last_failure_reason: str | None,
        last_failure_at: str | None,
    ) -> None:
        self.consecutive_failures = consecutive_failures
        self.last_failure_reason = last_failure_reason
        self.last_failure_at = last_failure_at
        super().__init__(
            f"breaker tripped: {consecutive_failures} consecutive failures "
            f"(last_failure_reason={last_failure_reason!r}, last_failure_at={last_failure_at!r})"
        )


@dataclass
class BreakerState:
    """Mirrors the on-disk JSON schema exactly. `last_failure_reason` is stored as the enum's
    plain string value on disk (never the Python `Enum` repr) so the file stays plain JSON."""

    schema_version: int = SCHEMA_VERSION
    consecutive_failures: int = 0
    last_failure_at: str | None = None
    last_failure_reason: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> BreakerState:
        return cls(
            schema_version=int(raw.get("schema_version", SCHEMA_VERSION)),
            consecutive_failures=int(raw.get("consecutive_failures", 0)),
            last_failure_at=raw.get("last_failure_at"),
            last_failure_reason=raw.get("last_failure_reason"),
        )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write `data` to `path` atomically: build the full JSON in a temp file in the same
    directory, fsync it, then `os.replace()` over the real path. Never writes in-place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        os.chmod(tmp_name, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _read_state(path: Path) -> BreakerState:
    if not path.exists():
        return BreakerState()
    with path.open("r") as f:
        raw = json.load(f)
    return BreakerState.from_dict(raw)


class Breaker:
    """One breaker instance per `data/breaker_state.json` path. Construct with
    `max_consecutive_failures` from `Config` (default matches `Config.max_consecutive_failures`'s
    own default of 5)."""

    def __init__(
        self,
        path: str | Path = DEFAULT_BREAKER_PATH,
        max_consecutive_failures: int = 5,
    ) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.max_consecutive_failures = max_consecutive_failures

    @contextlib.contextmanager
    def _locked(self):
        """Holds an exclusive `flock` on a dedicated lock file for the whole read-modify-write
        cycle — see module docstring for why this is a separate file from the data file."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def check_tripped(self) -> BreakerState:
        """Read current state and raise `BreakerTripped` if `consecutive_failures` has reached
        `max_consecutive_failures`. Call this before attempting login — never after."""
        with self._locked():
            state = _read_state(self.path)
        if state.consecutive_failures >= self.max_consecutive_failures:
            raise BreakerTripped(
                state.consecutive_failures, state.last_failure_reason, state.last_failure_at
            )
        return state

    def record_failure(self, reason: FailureReason | str) -> BreakerState:
        """Increment `consecutive_failures` and record `reason`/timestamp. `reason` must be a
        `FailureReason` (or a string matching one of its values) — anything else raises
        `ValueError`, enforcing the closed set at write time, not just by convention."""
        reason = FailureReason(reason)
        with self._locked():
            state = _read_state(self.path)
            state = BreakerState(
                schema_version=SCHEMA_VERSION,
                consecutive_failures=state.consecutive_failures + 1,
                last_failure_at=_now_iso(),
                last_failure_reason=reason.value,
            )
            _atomic_write_json(self.path, state.to_dict())
            os.chmod(self.path, 0o600)
        return state

    def record_success(self) -> BreakerState:
        """Reset the breaker on a successful cycle: `consecutive_failures` back to 0, and
        `last_failure_at`/`last_failure_reason` cleared together with it — never leaves a stale
        failure reason sitting next to a 0 counter."""
        state = BreakerState(
            schema_version=SCHEMA_VERSION,
            consecutive_failures=0,
            last_failure_at=None,
            last_failure_reason=None,
        )
        with self._locked():
            _atomic_write_json(self.path, state.to_dict())
            os.chmod(self.path, 0o600)
        return state
