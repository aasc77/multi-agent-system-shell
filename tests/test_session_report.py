"""
Tests for orchestrator/session_report.py -- Session Report Generator

TDD Contract (RED phase):
These tests define the expected behavior of the Session Report module.
They MUST fail until the implementation is written.

Requirements traced to PRD:
  - R11: Logging -- session report at projects/<name>/session-report.md
  - R5: Task Queue -- track task assignments, completions, blockers

Acceptance criteria from task rgr-10:
  7. Session report written to projects/<name>/session-report.md
  8. Session report entries are timestamped markdown
  9. Session report includes task assignments, completions, and blockers

Test categories:
  1. Construction and initialization
  2. Report file path
  3. Task assignment entries
  4. Task completion entries
  5. Blocker entries
  6. Timestamp format
  7. Markdown structure
  8. Multiple entries
  9. Edge cases and error handling
"""

import os
import re
from datetime import datetime

import pytest

# --- The imports that MUST fail in RED phase ---
from orchestrator.session_report import SessionReport, SessionReportError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_project_dir(tmp_path):
    """Create a temporary project directory."""
    project_dir = tmp_path / "projects" / "demo"
    project_dir.mkdir(parents=True)
    return project_dir


@pytest.fixture
def report(tmp_project_dir):
    """Create a SessionReport instance for the demo project."""
    return SessionReport(
        project_name="demo",
        projects_dir=str(tmp_project_dir.parent),
    )


@pytest.fixture
def report_path(tmp_project_dir):
    """Return the expected report file path."""
    return str(tmp_project_dir / "session-report.md")


# ===========================================================================
# 1. CONSTRUCTION AND INITIALIZATION
# ===========================================================================


class TestConstruction:
    """SessionReport must be constructable with project info."""

    def test_create_session_report(self, tmp_project_dir):
        """SessionReport can be instantiated with project name and dir."""
        sr = SessionReport(
            project_name="demo",
            projects_dir=str(tmp_project_dir.parent),
        )
        assert sr is not None

    def test_project_name_stored(self, report):
        """SessionReport stores the project name."""
        assert report.project_name == "demo"

    def test_report_path_attribute(self, report, report_path):
        """SessionReport exposes the report file path."""
        assert report.report_path == report_path


# ===========================================================================
# 2. REPORT FILE PATH
# ===========================================================================


class TestReportFilePath:
    """Session report must be at projects/<name>/session-report.md."""

    def test_report_path_follows_convention(self, tmp_project_dir):
        """Report path must be projects/<name>/session-report.md."""
        sr = SessionReport(
            project_name="demo",
            projects_dir=str(tmp_project_dir.parent),
        )
        expected = os.path.join(str(tmp_project_dir.parent), "demo", "session-report.md")
        assert sr.report_path == expected

    def test_report_path_for_different_project(self, tmp_path):
        """Report path adjusts to project name."""
        projects_dir = tmp_path / "projects"
        (projects_dir / "other-project").mkdir(parents=True)
        sr = SessionReport(
            project_name="other-project",
            projects_dir=str(projects_dir),
        )
        expected = os.path.join(str(projects_dir), "other-project", "session-report.md")
        assert sr.report_path == expected

    def test_report_file_created_on_first_entry(self, report, report_path):
        """The report file should be created when the first entry is added."""
        report.log_task_assignment(task_id="task-1", agent="writer")
        assert os.path.exists(report_path)


# ===========================================================================
# 3. TASK ASSIGNMENT ENTRIES
# ===========================================================================


class TestTaskAssignmentEntries:
    """Session report must record task assignment events."""

    def test_log_task_assignment_basic(self, report, report_path):
        """log_task_assignment writes a task assignment entry."""
        report.log_task_assignment(task_id="task-1", agent="writer")

        with open(report_path, "r") as f:
            content = f.read()

        assert "task-1" in content
        assert "writer" in content

    def test_task_assignment_contains_assigned_keyword(self, report, report_path):
        """Assignment entry should indicate the assignment action."""
        report.log_task_assignment(task_id="task-1", agent="writer")

        with open(report_path, "r") as f:
            content = f.read()

        assert "assign" in content.lower()

    def test_task_assignment_with_title(self, report, report_path):
        """Task assignment entry can include a task title."""
        report.log_task_assignment(
            task_id="task-1", agent="writer", title="Do the thing"
        )

        with open(report_path, "r") as f:
            content = f.read()

        assert "Do the thing" in content

    def test_multiple_task_assignments(self, report, report_path):
        """Multiple assignments are appended to the report."""
        report.log_task_assignment(task_id="task-1", agent="writer")
        report.log_task_assignment(task_id="task-2", agent="executor")

        with open(report_path, "r") as f:
            content = f.read()

        assert "task-1" in content
        assert "task-2" in content
        assert "writer" in content
        assert "executor" in content


# ===========================================================================
# 4. TASK COMPLETION ENTRIES
# ===========================================================================


class TestTaskCompletionEntries:
    """Session report must record task completion events."""

    def test_log_task_completion_basic(self, report, report_path):
        """log_task_completion writes a task completion entry."""
        report.log_task_completion(task_id="task-1")

        with open(report_path, "r") as f:
            content = f.read()

        assert "task-1" in content

    def test_task_completion_contains_completed_keyword(self, report, report_path):
        """Completion entry should indicate the task was completed."""
        report.log_task_completion(task_id="task-1")

        with open(report_path, "r") as f:
            content = f.read()

        assert "complet" in content.lower()

    def test_task_completion_with_summary(self, report, report_path):
        """Task completion entry can include a summary."""
        report.log_task_completion(
            task_id="task-1", summary="All tests passed"
        )

        with open(report_path, "r") as f:
            content = f.read()

        assert "All tests passed" in content

    def test_task_completion_with_status(self, report, report_path):
        """Task completion entry can include status (pass/fail/stuck)."""
        report.log_task_completion(task_id="task-1", status="pass")

        with open(report_path, "r") as f:
            content = f.read()

        assert "pass" in content.lower()


# ===========================================================================
# 5. BLOCKER ENTRIES
# ===========================================================================


class TestBlockerEntries:
    """Session report must record blocker events."""

    def test_log_blocker_basic(self, report, report_path):
        """log_blocker writes a blocker entry."""
        report.log_blocker(task_id="task-2", reason="Max retries exceeded")

        with open(report_path, "r") as f:
            content = f.read()

        assert "task-2" in content

    def test_blocker_contains_blocked_keyword(self, report, report_path):
        """Blocker entry should indicate the task is blocked."""
        report.log_blocker(task_id="task-2", reason="Max retries exceeded")

        with open(report_path, "r") as f:
            content = f.read()

        assert "block" in content.lower() or "stuck" in content.lower()

    def test_blocker_includes_reason(self, report, report_path):
        """Blocker entry must include the reason for the block."""
        report.log_blocker(task_id="task-2", reason="Agent unresponsive after 20 nudge retries")

        with open(report_path, "r") as f:
            content = f.read()

        assert "Agent unresponsive after 20 nudge retries" in content

    def test_blocker_with_agent(self, report, report_path):
        """Blocker entry can include the affected agent."""
        report.log_blocker(
            task_id="task-2",
            reason="Max retries exceeded",
            agent="writer",
        )

        with open(report_path, "r") as f:
            content = f.read()

        assert "writer" in content


# ===========================================================================
# 6. TIMESTAMP FORMAT
# ===========================================================================


class TestTimestampFormat:
    """All session report entries must be timestamped."""

    def test_task_assignment_has_timestamp(self, report, report_path):
        """Task assignment entry must include a timestamp."""
        report.log_task_assignment(task_id="task-1", agent="writer")

        with open(report_path, "r") as f:
            content = f.read()

        # Should contain a date-like pattern: YYYY-MM-DD HH:MM:SS or similar
        pattern = r"\d{4}-\d{2}-\d{2}"
        assert re.search(pattern, content), f"No timestamp found in: {content}"

    def test_task_completion_has_timestamp(self, report, report_path):
        """Task completion entry must include a timestamp."""
        report.log_task_completion(task_id="task-1")

        with open(report_path, "r") as f:
            content = f.read()

        pattern = r"\d{4}-\d{2}-\d{2}"
        assert re.search(pattern, content)

    def test_blocker_has_timestamp(self, report, report_path):
        """Blocker entry must include a timestamp."""
        report.log_blocker(task_id="task-2", reason="Stuck")

        with open(report_path, "r") as f:
            content = f.read()

        pattern = r"\d{4}-\d{2}-\d{2}"
        assert re.search(pattern, content)

    def test_timestamp_includes_time_component(self, report, report_path):
        """Timestamp should include hours, minutes, and seconds."""
        report.log_task_assignment(task_id="task-1", agent="writer")

        with open(report_path, "r") as f:
            content = f.read()

        # HH:MM:SS pattern
        pattern = r"\d{2}:\d{2}:\d{2}"
        assert re.search(pattern, content), f"No time component found in: {content}"

    def test_timestamps_are_sequential(self, report, report_path):
        """Multiple entries should have sequential timestamps."""
        import time
        report.log_task_assignment(task_id="task-1", agent="writer")
        time.sleep(0.01)  # Tiny delay to ensure distinct timestamps
        report.log_task_completion(task_id="task-1")

        with open(report_path, "r") as f:
            content = f.read()

        # Find all timestamps
        timestamps = re.findall(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", content)
        assert len(timestamps) >= 2


# ===========================================================================
# 7. MARKDOWN STRUCTURE
# ===========================================================================


class TestMarkdownStructure:
    """Session report must be valid markdown."""

    def test_report_has_markdown_header(self, report, report_path):
        """Report should start with a markdown header."""
        report.log_task_assignment(task_id="task-1", agent="writer")

        with open(report_path, "r") as f:
            content = f.read()

        # Should start with a markdown heading (# or ##)
        assert content.strip().startswith("#")

    def test_report_header_contains_project_name(self, report, report_path):
        """Report header should include the project name."""
        report.log_task_assignment(task_id="task-1", agent="writer")

        with open(report_path, "r") as f:
            content = f.read()

        first_line = content.strip().split("\n")[0]
        assert "demo" in first_line.lower() or "session" in first_line.lower()

    def test_entries_use_markdown_list_or_heading(self, report, report_path):
        """Each entry should use markdown formatting (list items or subheadings)."""
        report.log_task_assignment(task_id="task-1", agent="writer")
        report.log_task_completion(task_id="task-1")

        with open(report_path, "r") as f:
            content = f.read()

        # Entries should use markdown lists (- or *) or headings (##)
        lines = content.strip().split("\n")
        # At least some lines should start with markdown formatting
        formatted_lines = [
            l for l in lines
            if l.strip().startswith("-")
            or l.strip().startswith("*")
            or l.strip().startswith("#")
            or l.strip().startswith("|")
        ]
        assert len(formatted_lines) >= 2  # header + at least one entry

    def test_file_extension_is_md(self, report):
        """Report file must have .md extension."""
        assert report.report_path.endswith(".md")


# ===========================================================================
# 8. MULTIPLE ENTRIES (INTEGRATION-LIKE)
# ===========================================================================


class TestMultipleEntries:
    """Session report supports a realistic mix of entries."""

    def test_full_session_workflow(self, report, report_path):
        """A full workflow: assign, complete, assign, block."""
        report.log_task_assignment(task_id="task-1", agent="writer")
        report.log_task_completion(task_id="task-1", status="pass")
        report.log_task_assignment(task_id="task-2", agent="writer")
        report.log_blocker(task_id="task-2", reason="Max retries exceeded")

        with open(report_path, "r") as f:
            content = f.read()

        assert "task-1" in content
        assert "task-2" in content
        assert "writer" in content
        assert "complet" in content.lower()
        assert "block" in content.lower() or "stuck" in content.lower()

    def test_entries_appear_in_chronological_order(self, report, report_path):
        """Entries should appear in the order they were logged."""
        report.log_task_assignment(task_id="task-1", agent="writer")
        report.log_task_completion(task_id="task-1")
        report.log_task_assignment(task_id="task-2", agent="executor")

        with open(report_path, "r") as f:
            content = f.read()

        # task-1 assignment should appear before task-1 completion
        # and both should appear before task-2 assignment
        pos_assign_1 = content.find("task-1")
        pos_assign_2 = content.find("task-2")
        assert pos_assign_1 < pos_assign_2

    def test_many_entries_do_not_overwrite(self, report, report_path):
        """Adding many entries should append, not overwrite previous ones."""
        for i in range(10):
            report.log_task_assignment(task_id=f"task-{i}", agent="writer")

        with open(report_path, "r") as f:
            content = f.read()

        for i in range(10):
            assert f"task-{i}" in content


# ===========================================================================
# 9. EDGE CASES AND ERROR HANDLING
# ===========================================================================


class TestEdgeCases:
    """Edge cases for the session report."""

    def test_creates_project_directory_if_missing(self, tmp_path):
        """SessionReport should create the project dir if it doesn't exist."""
        projects_dir = str(tmp_path / "projects")
        sr = SessionReport(
            project_name="new-project",
            projects_dir=projects_dir,
        )
        sr.log_task_assignment(task_id="task-1", agent="writer")

        expected_path = os.path.join(projects_dir, "new-project", "session-report.md")
        assert os.path.exists(expected_path)

    def test_special_characters_in_task_id(self, report, report_path):
        """Task IDs with special characters should be handled."""
        report.log_task_assignment(task_id="task-1/sub-a", agent="writer")

        with open(report_path, "r") as f:
            content = f.read()

        assert "task-1/sub-a" in content

    def test_empty_reason_for_blocker(self, report, report_path):
        """Blocker with empty reason should still log."""
        report.log_blocker(task_id="task-1", reason="")

        with open(report_path, "r") as f:
            content = f.read()

        assert "task-1" in content

    def test_long_summary_in_completion(self, report, report_path):
        """Long summary text should be fully captured."""
        long_summary = "A" * 500
        report.log_task_completion(task_id="task-1", summary=long_summary)

        with open(report_path, "r") as f:
            content = f.read()

        assert long_summary in content

    def test_unicode_in_entries(self, report, report_path):
        """Unicode characters should be handled correctly."""
        report.log_task_assignment(task_id="tarea-1", agent="escritor")
        report.log_blocker(task_id="tarea-1", reason="Error: conexion perdida")

        with open(report_path, "r") as f:
            content = f.read()

        assert "tarea-1" in content
        assert "escritor" in content
        assert "conexion perdida" in content

    def test_session_report_error_on_invalid_projects_dir(self):
        """SessionReportError should be raised for truly invalid operations."""
        # The error class should exist for exceptional situations
        assert SessionReportError is not None
        err = SessionReportError("test error")
        assert str(err) == "test error"

    def test_report_survives_rapid_sequential_writes(self, report, report_path):
        """Rapid sequential writes should all be captured."""
        for i in range(50):
            report.log_task_assignment(task_id=f"rapid-{i}", agent="writer")

        with open(report_path, "r") as f:
            content = f.read()

        for i in range(50):
            assert f"rapid-{i}" in content

    def test_report_append_mode_after_manual_edit(self, report, report_path):
        """If the report file exists with prior content, new entries append."""
        # Pre-create the file with some content
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        with open(report_path, "w") as f:
            f.write("# Pre-existing content\n\n- Old entry\n")

        report.log_task_assignment(task_id="task-new", agent="writer")

        with open(report_path, "r") as f:
            content = f.read()

        # Both old and new content should be present
        assert "Old entry" in content
        assert "task-new" in content
