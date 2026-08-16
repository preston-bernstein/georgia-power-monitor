"""Node-exporter textfile-collector metrics — home-infra CONVENTIONS.md §18.

This package is a systemd ``Type=oneshot`` job (gp-monitor). A oneshot job cannot be scraped — there
is no process listening at the moment a Prometheus scrape would arrive, because the job has already
exited by then. §18's mechanism for this deployment shape is the node-exporter **textfile collector**:
write a ``.prom`` file under the shared textfile directory on the way out; node-exporter picks it up
on its own next scrape.

The write itself is tmp-file-then-atomic-``os.replace``, the same pattern proven in home-infra's
``arr-stack/gluetun-health-metric.sh`` — node-exporter must never observe a half-written file, since
a malformed ``.prom`` silences its *entire* textfile collector, not just this one service's metrics.

Exports, at minimum per §18's "the minimum metric set" — labeled ``phase="poll"``:
  * ``gp_monitor_last_run_timestamp_seconds`` — staleness (only updated on success).
  * ``gp_monitor_last_run_success`` — 1/0; updated on every run.
  * ``gp_monitor_work_quantity`` — total kWh collected on last run.
  * ``gp_monitor_work_available`` — billing dollars available on last run.
"""

from __future__ import annotations

import os
import time

DEFAULT_TEXTFILE_DIR = "/opt/docker/observability/node-exporter-textfiles"
TEXTFILE_DIR_ENV = "GP_MONITOR_TEXTFILE_DIR"
METRIC_FILE_NAME = "gp_monitor.prom"

_HELP: dict[str, str] = {
    "gp_monitor_last_run_timestamp_seconds": (
        "Unix timestamp of the last successful run of the poll phase."
    ),
    "gp_monitor_last_run_success": (
        "1 if the last run of the poll phase completed successfully, 0 otherwise."
    ),
    "gp_monitor_work_quantity": (
        "Total kWh collected on the last successful run."
    ),
    "gp_monitor_work_available": (
        "Billing dollars available on the last successful run."
    ),
}
_METRIC_NAMES = tuple(_HELP)


def _textfile_path(textfile_dir: str | None) -> tuple[str, str]:
    directory = textfile_dir or os.environ.get(TEXTFILE_DIR_ENV, DEFAULT_TEXTFILE_DIR)
    return directory, os.path.join(directory, METRIC_FILE_NAME)


def _parse_existing(path: str) -> dict[str, float]:
    """Parse existing metrics from disk.

    Returns {metric_name: value} for metrics this module owns.
    Returns empty dict if file doesn't exist or can't be read.
    """
    data: dict[str, float] = {}
    if not os.path.exists(path):
        return data
    try:
        with open(path) as fh:
            for raw_line in fh:
                line = raw_line.strip()
                # Match: metric_name{phase="poll"} value
                if (
                    line.startswith("gp_monitor_")
                    and "{phase=\"poll\"}" in line
                ):
                    parts = line.split("}")
                    if len(parts) >= 2:
                        metric_name = parts[0].split("{")[0]
                        try:
                            value = float(parts[1].strip())
                            if metric_name in _METRIC_NAMES:
                                data[metric_name] = value
                        except (ValueError, IndexError):
                            continue
    except OSError:
        pass
    return data


def _render(data: dict[str, float]) -> str:
    """Render metrics dict to Prometheus exposition format."""
    out: list[str] = []
    for name in _METRIC_NAMES:
        if name not in data:
            continue
        out.append(f"# HELP {name} {_HELP[name]}")
        out.append(f"# TYPE {name} gauge")
        out.append(f'{name}{{phase="poll"}} {data[name]}')
    return "\n".join(out) + ("\n" if out else "")


def write_metrics(
    *,
    success: bool,
    work_quantity: float,
    work_available: float,
    textfile_dir: str | None = None,
    now: float | None = None,
) -> str:
    """Write metrics to the textfile, atomically.

    Returns the path written, or ``""`` if the textfile directory does not exist (a dev box or a
    test sandbox without the observability stack deployed) — a missing directory is never fatal to
    the CLI command that called this; metrics coverage is a deploy-time concern, not a reason for
    the main job to fail on a laptop.

    On success: updates all four metrics (timestamp, success flag, work_quantity, work_available).
    On failure: preserves timestamp from previous run (staleness is visible in Prometheus),
               updates success flag to 0, does not update work quantities.
    """
    directory, path = _textfile_path(textfile_dir)
    if not os.path.isdir(directory):
        return ""

    # Read existing metrics to preserve timestamp on failure
    data = _parse_existing(path)

    if success:
        # Update all metrics on success
        data["gp_monitor_last_run_timestamp_seconds"] = (
            now if now is not None else time.time()
        )
        data["gp_monitor_work_quantity"] = work_quantity
        data["gp_monitor_work_available"] = work_available
    else:
        # On failure: preserve timestamp (don't update it), only update success flag
        # Remove work quantities so they don't appear in output
        data.pop("gp_monitor_work_quantity", None)
        data.pop("gp_monitor_work_available", None)

    data["gp_monitor_last_run_success"] = 1 if success else 0

    tmp_path = f"{path}.tmp.{os.getpid()}"
    with open(tmp_path, "w") as fh:
        fh.write(_render(data))
    os.replace(tmp_path, path)
    return path
