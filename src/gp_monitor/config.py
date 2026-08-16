"""Runtime configuration loader.

Thin wrapper around the shared `fleet-logging` package's `load_config(dataclass_type, path)` (see
the git-pinned dependency in pyproject.toml). Follows `internal-monitor-service/src/macro_monitor/config.py`'s
pattern verbatim (plan.md's Integration points, FR-10) — a flat dataclass, `config.yaml` (gitignored)
overlay with env-var override, non-fatal on a missing file (falls back to defaults), fatal on a file
that exists but fails to parse. `env_prefix`/`SERVICE`/the field set are adapted to this repo.

`env_prefix="GP_MONITOR_"` is passed explicitly here as a deliberate safety choice, same reasoning
as `macro_monitor/config.py`: `fleet_logging.load_config` overlays a bare uppercased env var per
field (`HA_BASE_URL`, `POLL_INTERVAL_SECONDS`, ...) by default. This service runs live under
systemd with an `EnvironmentFile=` that also sets `GP_USERNAME`/`GP_PASSWORD`/`HA_LONG_LIVED_TOKEN`
(read directly from `os.environ` by `auth.py`/`publish.py`, never through this loader — see below);
prefixing this loader's own overlay keeps its namespace from ever colliding with those or any other
generic-sounding env var.

`fleet_logging.load_config`'s own internal `config.missing`/`config.parse_failed` log lines have no
`stream=` override (unlike `fleet_logging.log_event`, which does) — they always resolve
`sys.stdout` at call time. That's wrong for this repo: every machine-readable §18 log line belongs
on **stderr**, kept deliberately separate from `click.echo`'s stdout (see `log.py`'s module
docstring). `_log_config_events_to_stderr` below closes that gap the only way the public API
allows: it temporarily points `sys.stdout` at the real `sys.stderr` for the duration of the single,
synchronous `load_config()` call this module makes (the first thing `cli.py`'s `poll` command does,
before any `click.echo` output exists to collide with), then restores it immediately after.

`fleet_logging.load_config` also unconditionally calls python-dotenv's `load_dotenv()` unless a
`dotenv_path=` is given — with no path, that walks *up the filesystem tree from wherever the
`fleet_logging` package itself is installed* (not this repo's cwd) looking for any file literally
named `.env`, and silently overlays whatever it finds into `os.environ` for the rest of the
process. This repo's secrets (`GP_USERNAME`, `GP_PASSWORD`, `HA_LONG_LIVED_TOKEN`) come only from
the process environment via systemd's `EnvironmentFile=` (FR-11) — never via python-dotenv
tree-walking. `_NO_DOTENV` below pins an explicitly nonexistent path, which short-circuits
`load_dotenv()` straight to its does-nothing branch, so this stays a no-op instead of an
undocumented tree-walking side effect on every live invocation (the stealth-credential guard the
task description refers to).

Validation (plan.md's Integration points / config.example.yaml section): `poll_interval_seconds`,
`max_login_attempts`, and `max_consecutive_failures` must all be positive — a 0 or negative value
fails fast in `Config.__post_init__` below (fleet_logging.load_config constructs `Config` via a
plain `Config(**kwargs)` call, so `__post_init__` always runs, whether values came from config.yaml,
an env override, or a dataclass default). `ha_base_url` is checked against a hostname
allowlist/format check at load time — scheme must be http/https, and the host must be a
private-use/loopback IP address or a `.local`/`localhost` name — so a misconfigured value can't
silently exfiltrate the HA long-lived Bearer token to an unintended (public) host. Entity-id
overrides are validated against Home Assistant's `domain.object_id` character set.
"""

from __future__ import annotations

import contextlib
import ipaddress
import os
import re
import sys
from dataclasses import dataclass
from urllib.parse import urlparse

from fleet_logging import load_config as _fleet_load_config

from .log import SERVICE

DEFAULT_CONFIG_PATH = "config.yaml"

# See module docstring. A path guaranteed never to exist, so `fleet_logging.load_config`'s
# internal `load_dotenv(dotenv_path)` call is always a no-op instead of the tree-walking
# `load_dotenv()` (no args) default.
_NO_DOTENV = os.path.join(os.devnull, "unused.env")

# Home Assistant's entity_id character set: lowercase ASCII letters, digits, and underscores,
# split as `domain.object_id` (each half must start with a letter, never a digit or underscore).
_ENTITY_ID_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")

# Hostnames allowed for `ha_base_url` beyond a private-use/loopback IP literal — see
# `_validate_ha_base_url`.
_ALLOWED_HA_HOSTS = {"localhost"}
_ALLOWED_HA_HOST_SUFFIXES = (".local",)


@contextlib.contextmanager
def _log_config_events_to_stderr():
    """See module docstring. Narrow, restored-in-`finally` redirect around one call only."""
    original_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        yield
    finally:
        sys.stdout = original_stdout


def _validate_positive(name: str, value: int) -> None:
    if value <= 0:
        raise ValueError(f"config.invalid: {name} must be a positive integer, got {value!r}")


def _validate_ha_base_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"config.invalid: ha_base_url must use http:// or https://, got {value!r}"
        )
    host = parsed.hostname
    if not host:
        raise ValueError(f"config.invalid: ha_base_url has no host, got {value!r}")

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None

    if ip is not None:
        if ip.is_private or ip.is_loopback:
            return
        raise ValueError(
            f"config.invalid: ha_base_url host {host!r} is not a private/loopback address "
            "(refusing to send the HA Bearer token to a public host)"
        )

    if host in _ALLOWED_HA_HOSTS or host.endswith(_ALLOWED_HA_HOST_SUFFIXES):
        return
    raise ValueError(
        f"config.invalid: ha_base_url host {host!r} is not allowlisted "
        "(must be a private/loopback IP, 'localhost', or a '*.local' name)"
    )


def _validate_entity_id(field_name: str, value: str) -> None:
    if not _ENTITY_ID_RE.match(value):
        raise ValueError(
            f"config.invalid: {field_name} {value!r} is not a valid Home Assistant entity_id "
            "(expected 'domain.object_id', lowercase letters/digits/underscores only)"
        )


@dataclass
class Config:
    # Home Assistant's base URL (config, not secret — the long-lived token is the secret, read
    # from the process environment by publish.py). Default matches the documented deploy target
    # (plan.md's API / interface contract) — a private LAN address, valid under
    # `_validate_ha_base_url` with zero config.yaml present.
    ha_base_url: str = "http://ha.example.internal:8123"
    poll_interval_seconds: int = 86400
    max_login_attempts: int = 2
    max_consecutive_failures: int = 5

    entity_id_usage: str = "sensor.georgia_power_usage_kwh"
    entity_id_bill: str = "sensor.georgia_power_bill_to_date"
    entity_id_last_poll: str = "sensor.georgia_power_last_poll"
    friendly_name_usage: str = "Georgia Power Usage (Month to Date)"
    friendly_name_bill: str = "Georgia Power Bill (Month to Date)"
    friendly_name_last_poll: str = "Georgia Power Last Poll"

    def __post_init__(self) -> None:
        _validate_positive("poll_interval_seconds", self.poll_interval_seconds)
        _validate_positive("max_login_attempts", self.max_login_attempts)
        _validate_positive("max_consecutive_failures", self.max_consecutive_failures)
        _validate_ha_base_url(self.ha_base_url)
        _validate_entity_id("entity_id_usage", self.entity_id_usage)
        _validate_entity_id("entity_id_bill", self.entity_id_bill)
        _validate_entity_id("entity_id_last_poll", self.entity_id_last_poll)


def load_config(path: str | None = None) -> Config:
    """Load ``config.yaml`` (or `path`) into a `Config`.

    Non-fatal on a missing file — falls back to defaults, after logging a `config.missing` warning
    with the exact resolved path that was tried (so a fresh checkout or a mis-pathed config is
    never silently empty). Fatal (re-raised, after logging `config.parse_failed`) on a file that
    exists but fails to parse — a malformed config is a genuine operator error, not something to
    silently default around. Both behaviors are `fleet_logging.load_config`'s own default
    (`required=False`).

    Raises `ValueError` (from `Config.__post_init__`) if any loaded value fails validation — see
    module docstring.
    """
    with _log_config_events_to_stderr():
        return _fleet_load_config(
            Config,
            path or DEFAULT_CONFIG_PATH,
            required=False,
            env_prefix="GP_MONITOR_",
            service=SERVICE,
            dotenv_path=_NO_DOTENV,
        )
