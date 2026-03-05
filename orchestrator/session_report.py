"""Session report generator for the orchestrator.

Writes timestamped markdown entries for task assignments, completions,
and blockers to ``projects/<name>/session-report.md``.

Requirements
------------
- **R11** – Logging: session report at ``projects/<name>/session-report.md``.
- **R5**  – Task Queue: track task assignments, completions, and blockers.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_REPORT_FILENAME: str = "session-report.md"
"""Default filename for the markdown session report."""

_TIMESTAMP_FORMAT: str = "%Y-%m-%d %H:%M:%S"
"""strftime format used for entry timestamps (``YYYY-MM-DD HH:MM:SS``)."""

_HEADER_TEMPLATE: str = "# Session Report -- {project_name}\n\n"
"""Markdown header written when a new report file is created."""

_ENCODING: str = "utf-8"
"""Character encoding for report file I/O."""


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SessionReportError(Exception):
    """Raised when a session report operation fails."""


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------


class SessionReport:
    """Append-only markdown session report for a project.

    Each public ``log_*`` method writes a single timestamped markdown
    list entry to the report file.  The file (and its parent directory)
    is created lazily on the first write.

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
            projects_dir, project_name, _REPORT_FILENAME,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def log_task_assignment(
        self,
        *,
        task_id: str,
        agent: str,
        title: Optional[str] = None,
    ) -> None:
        """Append a task-assignment entry.

        Format: ``Task assigned: <task_id> -> <agent> [<title>]``

        Traced to PRD **R5** (Task Queue).
        """
        entry = f"Task assigned: {task_id} -> {agent}"
        if title is not None:
            entry += f" [{title}]"
        self._append_entry(entry)

    def log_task_completion(
        self,
        *,
        task_id: str,
        summary: Optional[str] = None,
        status: Optional[str] = None,
    ) -> None:
        """Append a task-completion entry.

        Format: ``Task completed: <task_id> [(status=<s>)] [-- <summary>]``

        Traced to PRD **R5** (Task Queue).
        """
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
        agent: Optional[str] = None,
    ) -> None:
        """Append a blocker entry.

        Format: ``Blocked: <task_id> [(agent=<a>)] -- <reason>``

        Traced to PRD **R5** (Task Queue).
        """
        entry = f"Blocked: {task_id}"
        if agent is not None:
            entry += f" (agent={agent})"
        entry += f" -- {reason}"
        self._append_entry(entry)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_file(self) -> None:
        """Create the report file with a markdown header if it does not exist."""
        report_dir = os.path.dirname(self.report_path)
        os.makedirs(report_dir, exist_ok=True)

        if not os.path.exists(self.report_path):
            with open(self.report_path, "w", encoding=_ENCODING) as fh:
                fh.write(_HEADER_TEMPLATE.format(project_name=self.project_name))

    def _append_entry(self, text: str) -> None:
        """Append a timestamped markdown list entry to the report file."""
        self._ensure_file()
        timestamp = datetime.now().strftime(_TIMESTAMP_FORMAT)
        line = f"- **[{timestamp}]** {text}\n"
        with open(self.report_path, "a", encoding=_ENCODING) as fh:
            fh.write(line)
