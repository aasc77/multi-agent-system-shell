"""Session report generator for the orchestrator.

Writes timestamped markdown entries for task assignments, completions,
and blockers to ``projects/<name>/session-report.md``.

Requirements: PRD R11, R5.
"""

from __future__ import annotations

import os
from datetime import datetime


class SessionReportError(Exception):
    """Raised when a session report operation fails."""


class SessionReport:
    """Append-only markdown session report for a project.

    Parameters
    ----------
    project_name:
        Name of the project (used in the directory path and report header).
    projects_dir:
        Root directory containing project sub-directories.
    """

    def __init__(self, project_name: str, projects_dir: str) -> None:
        self.project_name: str = project_name
        self._projects_dir: str = projects_dir
        self.report_path: str = os.path.join(
            projects_dir, project_name, "session-report.md"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def log_task_assignment(
        self,
        *,
        task_id: str,
        agent: str,
        title: str | None = None,
    ) -> None:
        """Append a task assignment entry."""
        entry = f"Task assigned: {task_id} -> {agent}"
        if title is not None:
            entry += f" [{title}]"
        self._append_entry(entry)

    def log_task_completion(
        self,
        *,
        task_id: str,
        summary: str | None = None,
        status: str | None = None,
    ) -> None:
        """Append a task completion entry."""
        entry = f"Task completed: {task_id}"
        if status is not None:
            entry += f" (status={status})"
        if summary is not None:
            entry += f" -- {summary}"
        self._append_entry(entry)

    def log_blocker(
        self,
        *,
        task_id: str,
        reason: str,
        agent: str | None = None,
    ) -> None:
        """Append a blocker entry."""
        entry = f"Blocked: {task_id}"
        if agent is not None:
            entry += f" (agent={agent})"
        entry += f" -- {reason}"
        self._append_entry(entry)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_file(self) -> None:
        """Create the report file with a header if it does not exist."""
        report_dir = os.path.dirname(self.report_path)
        os.makedirs(report_dir, exist_ok=True)

        if not os.path.exists(self.report_path):
            with open(self.report_path, "w", encoding="utf-8") as fh:
                fh.write(f"# Session Report -- {self.project_name}\n\n")

    def _append_entry(self, text: str) -> None:
        """Append a timestamped markdown list entry to the report file."""
        self._ensure_file()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"- **[{timestamp}]** {text}\n"
        with open(self.report_path, "a", encoding="utf-8") as fh:
            fh.write(line)
