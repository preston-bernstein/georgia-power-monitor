#!/usr/bin/env python3
"""Deploy-time preflight smoke check for gp-monitor.

Standalone script -- not part of the Click CLI (`gp_monitor.cli`) -- run manually after a deploy,
or auto-invoked as the final smoke step of `scripts/deploy.sh`. Validates two things about the
Home Assistant target, and nothing else:

  (a) HA reachability -- GET <ha_base_url>/api/ (no auth), expect any HTTP response (a real HA
      instance returns 401 unauthenticated -- that still proves it's up).
  (b) HA token validity -- GET <ha_base_url>/api/states with the long-lived Bearer token, expect 2xx.

Deliberately does NOT check egress/proxy connectivity and NEVER imports or calls anything related
to Georgia Power / the `southern_company_api` package (no login, no `get_month_data`, nothing).
Egress isolation (routing this integration's traffic through a shared VPN gateway) was dropped
during adversarial spec review -- routing a real personal utility account's traffic through a
shared gateway was judged to increase, not decrease, lockout risk. This script only ever talks to
the configured Home Assistant instance.

Security: never echoes an HTTP response body (either check's response could reflect the Bearer
token back, e.g. an error payload) and never surfaces a raw exception's `str()` -- the same
discipline `publish.py`/`log.py` already hold for the live poll path (see their module docstrings).
Failure messages here are built only from closed-set diagnostic text and bare HTTP status codes.

Exit codes: 0 if both checks pass, 1 if either fails.
"""

from __future__ import annotations

import argparse
import os
import sys

import httpx
from dotenv import dotenv_values

from gp_monitor.config import load_config
from gp_monitor.publish import HA_TOKEN_ENV_VAR

_TIMEOUT_SECONDS = 10.0


def _load_token(env_file: str | None) -> str | None:
    """Resolve the HA long-lived token.

    Prefers an explicit `--env-file` (if given and it exists), falling back to whatever is already
    in the process environment (e.g. a real systemd `EnvironmentFile=` already sourced). Reads the
    file at the caller-given path only, via `dotenv_values` (which returns a plain dict and never
    touches `os.environ`) -- never `load_dotenv()`'s no-args tree-walk. See `config.py`'s module
    docstring for why an implicit tree-walk is unacceptable for this repo's secrets; an explicit,
    caller-supplied path for a deploy-time diagnostic tool is a different, safe case.
    """
    if env_file and os.path.exists(env_file):
        values = dotenv_values(env_file)
        token = values.get(HA_TOKEN_ENV_VAR)
        if token:
            return token
    return os.environ.get(HA_TOKEN_ENV_VAR)


def _check_ha_reachable(client: httpx.Client, base_url: str) -> tuple[bool, str]:
    """Check (a): GET <base_url>/api/ (no auth header), expect any HTTP response. Never touches
    Georgia Power.

    This deliberately does NOT require a 2xx status. A real, healthy Home Assistant instance
    returns 401 for an unauthenticated request to `/api/` -- that response still proves the host
    is reachable and speaking HTTP. Only a connection-level failure (timeout, refused connection,
    DNS failure) means "not reachable"; check (b) below is what judges the token itself.
    """
    url = f"{base_url.rstrip('/')}/api/"
    try:
        response = client.get(url, timeout=_TIMEOUT_SECONDS)
    except httpx.TimeoutException:
        return False, f"timed out contacting {url}"
    except httpx.HTTPError:
        return False, f"could not connect to {url}"

    return True, f"HA reachable at {url} (HTTP {response.status_code})"


def _check_token_valid(client: httpx.Client, base_url: str, token: str | None) -> tuple[bool, str]:
    """Check (b): GET <base_url>/api/states with a Bearer token, expect 2xx. Never touches Georgia
    Power. Never logs the token or any response body."""
    url = f"{base_url.rstrip('/')}/api/states"
    if not token:
        return False, f"no {HA_TOKEN_ENV_VAR} found in the environment or --env-file"

    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = client.get(url, headers=headers, timeout=_TIMEOUT_SECONDS)
    except httpx.TimeoutException:
        return False, f"timed out contacting {url}"
    except httpx.HTTPError:
        return False, f"could not connect to {url}"

    if 200 <= response.status_code < 300:
        return True, f"token accepted at {url} (HTTP {response.status_code})"
    if response.status_code in (401, 403):
        return False, f"token rejected at {url} (HTTP {response.status_code} -- check {HA_TOKEN_ENV_VAR})"
    return False, f"got HTTP {response.status_code} from {url}"


def run_preflight(
    config_path: str, env_file: str | None, *, client: httpx.Client | None = None
) -> bool:
    """Run both checks, printing one PASS/FAIL line per check. Returns True iff both passed.

    `client` lets tests supply an `httpx.Client` built on `httpx.MockTransport`; when omitted, a
    short-lived real client is created and closed for this call only.
    """
    try:
        config = load_config(config_path)
    except Exception:
        print(
            f"FAIL: could not load config from {config_path!r} "
            "(see the config.parse_failed log line above for detail)",
            file=sys.stderr,
        )
        return False

    token = _load_token(env_file)

    owns_client = client is None
    http_client = client if client is not None else httpx.Client()
    try:
        reachable_ok, reachable_msg = _check_ha_reachable(http_client, config.ha_base_url)
        print(f"{'PASS' if reachable_ok else 'FAIL'}: HA reachability -- {reachable_msg}")

        token_ok, token_msg = _check_token_valid(http_client, config.ha_base_url, token)
        print(f"{'PASS' if token_ok else 'FAIL'}: HA token validity -- {token_msg}")
    finally:
        if owns_client:
            http_client.close()

    return reachable_ok and token_ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Deploy-time preflight for gp-monitor: verify Home Assistant is reachable and the "
            "configured long-lived token is valid. Never contacts Georgia Power."
        )
    )
    parser.add_argument(
        "--config", default="config.yaml", help="Path to config.yaml (default: config.yaml)"
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Path to an env file providing HA_LONG_LIVED_TOKEN (falls back to the process environment)",
    )
    args = parser.parse_args(argv)

    ok = run_preflight(args.config, args.env_file)
    if ok:
        print("preflight: all checks passed")
        return 0
    print("preflight: one or more checks failed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
