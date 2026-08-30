"""Regression test: package source must never hard-code a real internal LAN address.

Before this test existed, `Config.ha_base_url` (src/gp_monitor/config.py) defaulted to
Preston's literal Home Assistant IP -- a real RFC1918 address baked into git-tracked,
publicly-readable source, alongside a matching real-address default in
`config.example.yaml`. That's exactly the "documented map of what runs where on which
IP" a public-readiness audit flags as HIGH severity (see the audit that caught this).

Any host/IP/port a real deployment needs must come from `config.yaml` (gitignored,
host-local) or a `GP_MONITOR_*` env var override -- never a real address shipped in the
package source itself. A loopback address (127.0.0.1) is the sanctioned safe default
and is deliberately allowed through: it's not RFC1918 and reveals nothing about
Preston's actual network.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "gp_monitor"

_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

_RFC1918_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def _is_rfc1918(candidate: str) -> bool:
    try:
        addr = ipaddress.ip_address(candidate)
    except ValueError:
        return False
    return any(addr in net for net in _RFC1918_NETS)


def test_no_rfc1918_address_literal_in_package_source():
    """Scans every .py file under src/gp_monitor for a literal 10.x/172.16-31.x/192.168.x
    address. Fails loudly with the offending file:match so a future default doesn't quietly
    reintroduce real internal topology into the source that gets published publicly."""
    offenders = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        text = path.read_text()
        for match in _IPV4_RE.finditer(text):
            literal = match.group(0)
            if _is_rfc1918(literal):
                offenders.append(f"{path.relative_to(SRC_ROOT.parents[1])}: {literal!r}")

    assert not offenders, (
        "found a literal RFC1918 (private LAN) address hard-coded in package source -- "
        "hosts/IPs/ports must be config.yaml- or GP_MONITOR_*-env-driven with a loopback "
        "default, never a real address baked into publicly-readable source: "
        + ", ".join(offenders)
    )
