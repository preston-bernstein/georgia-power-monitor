"""Tests for gp_monitor.metrics module."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from gp_monitor import metrics


class TestMetricsWrite:
    """Tests for write_metrics function."""

    def test_write_metrics_success(self, tmp_path: Path) -> None:
        """Verify all four metrics are written on success."""
        now = 1234567890.0
        path = metrics.write_metrics(
            success=True,
            work_quantity=42.5,
            work_available=100.75,
            textfile_dir=str(tmp_path),
            now=now,
        )

        assert path
        prom_file = tmp_path / metrics.METRIC_FILE_NAME
        assert prom_file.exists()

        content = prom_file.read_text()

        # Verify all four metrics are present
        assert "# HELP gp_monitor_last_run_timestamp_seconds" in content
        assert "# TYPE gp_monitor_last_run_timestamp_seconds gauge" in content
        assert f'gp_monitor_last_run_timestamp_seconds{{phase="poll"}} {now}' in content

        assert "# HELP gp_monitor_last_run_success" in content
        assert "# TYPE gp_monitor_last_run_success gauge" in content
        assert 'gp_monitor_last_run_success{phase="poll"} 1' in content

        assert "# HELP gp_monitor_work_quantity" in content
        assert "# TYPE gp_monitor_work_quantity gauge" in content
        assert 'gp_monitor_work_quantity{phase="poll"} 42.5' in content

        assert "# HELP gp_monitor_work_available" in content
        assert "# TYPE gp_monitor_work_available gauge" in content
        assert 'gp_monitor_work_available{phase="poll"} 100.75' in content

    def test_write_metrics_failure(self, tmp_path: Path) -> None:
        """Verify timestamp not updated on failure, but success metric still written."""
        # First write a success
        now1 = 1234567890.0
        metrics.write_metrics(
            success=True,
            work_quantity=42.5,
            work_available=100.75,
            textfile_dir=str(tmp_path),
            now=now1,
        )

        # Now write a failure
        metrics.write_metrics(
            success=False,
            work_quantity=0.0,
            work_available=0.0,
            textfile_dir=str(tmp_path),
            now=1234567900.0,  # Later time, should be ignored
        )

        prom_file = tmp_path / metrics.METRIC_FILE_NAME
        content = prom_file.read_text()

        # Timestamp should still be the original (success) time, not the failure time
        assert f'gp_monitor_last_run_timestamp_seconds{{phase="poll"}} {now1}' in content
        # Success flag should be 0
        assert 'gp_monitor_last_run_success{phase="poll"} 0' in content
        # work_quantity and work_available should NOT be present (failure doesn't update them)
        assert "gp_monitor_work_quantity" not in content
        assert "gp_monitor_work_available" not in content

    def test_metrics_file_permissions(self, tmp_path: Path) -> None:
        """Verify written file has mode 0o644."""
        metrics.write_metrics(
            success=True,
            work_quantity=42.5,
            work_available=100.75,
            textfile_dir=str(tmp_path),
        )

        prom_file = tmp_path / metrics.METRIC_FILE_NAME
        file_stat = prom_file.stat()
        mode = stat.S_IMODE(file_stat.st_mode)
        # File should be readable by owner and group/others
        # We expect 0o644 after umask is applied; check that it's world-readable
        assert mode & stat.S_IRUSR  # Owner read
        assert mode & stat.S_IRGRP  # Group read
        assert mode & stat.S_IROTH  # Other read

    def test_write_metrics_missing_directory(self) -> None:
        """Verify missing directory returns empty string and doesn't error."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            nonexistent = os.path.join(tmp_dir, "nonexistent")
            path = metrics.write_metrics(
                success=True,
                work_quantity=42.5,
                work_available=100.75,
                textfile_dir=nonexistent,
            )
            assert path == ""

    def test_write_metrics_default_timestamp(self, tmp_path: Path) -> None:
        """Verify write_metrics uses current time when now is None."""
        path = metrics.write_metrics(
            success=True,
            work_quantity=42.5,
            work_available=100.75,
            textfile_dir=str(tmp_path),
            now=None,
        )

        assert path
        prom_file = tmp_path / metrics.METRIC_FILE_NAME
        content = prom_file.read_text()

        # Should have a timestamp line with some numeric value
        assert 'gp_monitor_last_run_timestamp_seconds{phase="poll"}' in content

    def test_prometheus_format_validity(self, tmp_path: Path) -> None:
        """Verify output is valid Prometheus exposition format."""
        metrics.write_metrics(
            success=True,
            work_quantity=42.5,
            work_available=100.75,
            textfile_dir=str(tmp_path),
            now=1234567890.0,
        )

        prom_file = tmp_path / metrics.METRIC_FILE_NAME
        lines = prom_file.read_text().strip().split("\n")

        # Each metric should have HELP, TYPE, and data line
        # Count them
        help_lines = [line for line in lines if line.startswith("# HELP")]
        type_lines = [line for line in lines if line.startswith("# TYPE")]
        data_lines = [
            line
            for line in lines
            if not line.startswith("#") and line  # Non-comment, non-empty
        ]

        # On success, we should have 4 metrics
        assert len(help_lines) == 4
        assert len(type_lines) == 4
        assert len(data_lines) == 4

    def test_textfile_dir_from_env(self, monkeypatch, tmp_path: Path) -> None:
        """Verify TEXTFILE_DIR_ENV is used when set."""
        monkeypatch.setenv(metrics.TEXTFILE_DIR_ENV, str(tmp_path))

        path = metrics.write_metrics(
            success=True,
            work_quantity=42.5,
            work_available=100.75,
            textfile_dir=None,  # Force use of env var
        )

        assert path
        assert str(tmp_path) in path
        prom_file = tmp_path / metrics.METRIC_FILE_NAME
        assert prom_file.exists()

    def test_atomic_write(self, tmp_path: Path) -> None:
        """Verify atomic write using tmp file and replace."""
        # This is a simple test that verifies the tmp file doesn't leak
        metrics.write_metrics(
            success=True,
            work_quantity=42.5,
            work_available=100.75,
            textfile_dir=str(tmp_path),
        )

        # Check no tmp files left behind
        files = list(tmp_path.glob("gp_monitor.prom*"))
        assert len(files) == 1
        assert files[0].name == metrics.METRIC_FILE_NAME
